import asyncio
import difflib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import typer
import uvicorn
from fastapi.encoders import jsonable_encoder
from pydantic import SecretStr, ValidationError
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as escape_markup
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlalchemy import func, select

from app.clipboard import (
    ClipboardImageError,
    read_clipboard_image,
)
from app.config import (
    LEGACY_ENV_BACKEND,
    Settings,
    default_config_path,
    get_settings,
    secret_value,
)
from app.connectors import (
    BrokerConfigurationError,
    MetaTraderBridgeError,
    OandaConnectorError,
    create_broker_connector,
    create_metatrader_connector,
    create_news_connector,
    create_oanda_connector,
    news_provider_configured,
)
from app.costs import (
    TokenUsage,
    calculate_cost,
    estimated_multi_round_usage,
    estimated_request_tokens,
    format_pricing,
    format_usd,
    model_pricing,
    output_budget_for_mode,
)
from app.db import (
    Base,
    LegacySchemaDetectedError,
    SessionLocal,
    adopt_legacy_database,
    engine,
    inspect_schema,
    schema_revisions,
    upgrade_database,
)
from app.integration_catalog import integration_options
from app.models import (
    ApiPrincipal,
    BrokerConnection,
    ConnectorCursor,
    ConversationSession,
    EconomicEvent,
)
from app.policy import ExecutionHooks, PolicyEngine, PolicyViolation, ToolContext
from app.providers import ProviderConfigurationError, create_model_provider
from app.providers.ollama_provider import OllamaProvider
from app.routing import AgentMode
from app.schemas import (
    AccountConstraintRead,
    AccountConstraintUpsert,
    AccountRuleLimits,
    BrokerPositionSizeRequest,
    InstrumentSpecificationCreate,
    ManagementEventCreate,
    MindsetCheckInCreate,
    PositionSizeRequest,
    ReflectionCreate,
    ReflectionRead,
    StrategyExperimentCreate,
    StrategyExperimentRead,
    StrategyTestSampleCreate,
    TradePlanCreate,
    TradePlanRead,
    TraderProfileUpsert,
)
from app.services.account_constraints import (
    account_rule_reminders,
    active_account_constraint,
    deactivate_account_constraints,
    unverified_account_rules,
    upsert_active_account_constraint,
)
from app.services.agent import (
    TOOLS,
    PreparedAgentRequest,
    TradingAgent,
    UsedReference,
    _chart_destination,
    _read_approved_chart,
)
from app.services.analytics import build_edge_report
from app.services.broker_credentials import (
    remove_broker_credential,
    retry_broker_secret_cleanup,
    rotate_broker_credential,
)
from app.services.broker_sync import synchronize_broker
from app.services.catalog import (
    active_instrument_specification,
    configure_account,
    configure_instrument_specification,
)
from app.services.chart_analysis import SYSTEM_PROMPT, analyze_chart
from app.services.chat_webhooks import set_chat_webhook_secret
from app.services.conversations import (
    add_turn,
    conversation_history,
    conversation_transcript,
    create_conversation,
    latest_conversation,
    list_conversations,
    resolve_conversation,
    update_turn_outcome,
)
from app.services.development import (
    DevelopmentService,
    DevelopmentSession,
    detect_development_intent,
    development_request,
)
from app.services.event_glossary import event_insight
from app.services.evidence import record_chart_analysis
from app.services.execution_ledger import record_management_event
from app.services.health import HealthReport, check_health
from app.services.integration_verification import (
    IntegrationVerification,
    integration_verifications,
    verify_live_integrations,
)
from app.services.journal import (
    ReflectionExistsError,
    TradeNotFoundError,
    create_reflection,
    create_trade_plan,
    get_trade_plan,
    list_trade_plans,
)
from app.services.knowledge_import import import_knowledge_path, import_knowledge_text
from app.services.learning import (
    TOPIC_LABELS,
    all_learning_topics,
    configure_learning_curriculum,
    curriculum_for_profile,
    curriculum_read,
    update_learning_module,
)
from app.services.market_features import (
    experiment_feature_correlations,
    measure_candle_features,
    strategy_experiment_report,
)
from app.services.mindset import create_mindset_check_in, list_mindset_check_ins
from app.services.news import (
    economic_event_history,
    store_calendar_events,
    store_news_items,
)
from app.services.pretrade import (
    PreflightAssessment,
    assess_preflight,
    detect_preflight_intent,
    instrument_event_currencies,
    news_readiness,
    persist_preflight_workflow,
    preflight_recall,
    pretrade_alerts,
    refresh_startup_calendar,
    render_pretrade_context,
    strategy_rules,
)
from app.services.principals import (
    create_principal,
    grant_principal,
    revoke_principal_grant,
    rotate_principal_token,
)
from app.services.profile_validation import validate_profile_text
from app.services.risk import calculate_broker_position_size, calculate_position_size
from app.services.secrets import SecretBackendError
from app.services.startup_memory import StartupMemory, build_startup_memory
from app.services.strategy_definitions import (
    canonical_strategy_definition,
    create_validated_strategy_version,
)
from app.services.strategy_workspace import (
    active_session_strategy,
    add_strategy_test_sample,
    complete_strategy_experiment,
    create_strategy_experiment,
    get_trader_profile,
    list_strategy_summaries,
    resolve_strategy_experiment,
    resolve_strategy_version,
    search_strategy_knowledge,
    set_session_strategy,
    set_strategy_knowledge_excluded,
    upsert_trader_profile,
)
from app.services.tool_audit import (
    complete_mutation_audit,
    record_direct_cli_confirmation,
)
from app.services.trading_workflow import infer_workflow_checkpoint
from app.services.tradingview import set_tradingview_webhook_secret
from app.services.workspaces import (
    RequestScope,
    bootstrap_initial_scope,
    list_accounts,
    resolve_account,
    resolve_current_scope,
    resolve_workspace,
)
from app.setup import (
    dependency_guidance,
    ensure_local_services,
    install_user_launcher,
    launcher_target_for_interpreter,
    ollama_profile_settings,
    provider_settings,
    pull_ollama_model,
    restore_env_file,
    shell_path_hint,
    snapshot_env_file,
    start_local_service,
    update_env_file,
)
from app.system_resources import (
    GIB,
    ModelFitAssessment,
    ResourceSnapshot,
    assess_model_fit,
    resource_snapshot,
)

app = typer.Typer(
    name="trading-agent",
    help="Journal-first, human-in-the-loop trading copilot.",
    no_args_is_help=False,
    invoke_without_command=True,
)
journal_app = typer.Typer(help="Create and inspect journal entries.")
app.add_typer(journal_app, name="journal")
sessions_app = typer.Typer(help="Inspect locally persisted agent conversations.")
app.add_typer(sessions_app, name="sessions")
database_app = typer.Typer(help="Inspect or migrate the PostgreSQL schema.")
app.add_typer(database_app, name="db")
broker_app = typer.Typer(help="Read-only broker configuration and synchronization.")
app.add_typer(broker_app, name="broker")
instrument_app = typer.Typer(help="Broker instrument specifications and deterministic sizing.")
app.add_typer(instrument_app, name="instrument")
edge_app = typer.Typer(help="Review measured setup performance.")
app.add_typer(edge_app, name="edge")
playbook_app = typer.Typer(help="Create immutable playbook versions.")
app.add_typer(playbook_app, name="playbook")
news_app = typer.Typer(help="Read and retain timestamped news/calendar metadata.")
app.add_typer(news_app, name="news")
develop_app = typer.Typer(help="Make isolated, testable changes to Trading Agent.")
app.add_typer(develop_app, name="develop")
strategy_app = typer.Typer(help="Create and select isolated strategy workspaces.")
app.add_typer(strategy_app, name="strategy")
knowledge_app = typer.Typer(help="Import and query strategy-scoped trading knowledge.")
app.add_typer(knowledge_app, name="knowledge")
experiment_app = typer.Typer(help="Track isolated backtests and forward tests.")
app.add_typer(experiment_app, name="experiment")
models_app = typer.Typer(help="Inspect, download, and select local Ollama models.")
app.add_typer(models_app, name="models")
mindset_app = typer.Typer(help="Record process readiness and predefined-risk acceptance.")
app.add_typer(mindset_app, name="mindset")
account_app = typer.Typer(help="List and select the account that scopes every decision.")
app.add_typer(account_app, name="account")
principal_app = typer.Typer(help="Provision hosted API principals and exact account grants.")
app.add_typer(principal_app, name="principal")
learn_app = typer.Typer(
    help="Follow a sourced trading curriculum or learn on demand.",
    invoke_without_command=True,
)
app.add_typer(learn_app, name="learn")
data_app = typer.Typer(help="See what Trading Agent has stored and how the data is organized.")
app.add_typer(data_app, name="data")
console = Console()


def _current_scope(db) -> RequestScope:
    settings = get_settings()
    try:
        return resolve_current_scope(
            db,
            workspace_reference=settings.trading_workspace,
            account_reference=settings.trading_account,
        )
    except LookupError as exc:
        raise LookupError(
            f"{exc}. Run `trade onboard` for a new installation, or "
            "`trade account list` to inspect available accounts"
        ) from exc


def _ensure_initial_scope(db, settings: Settings) -> RequestScope:
    """Resolve the selected scope or create only a genuinely empty first scope."""
    try:
        return resolve_current_scope(
            db,
            workspace_reference=getattr(settings, "trading_workspace", "legacy-local"),
            account_reference=getattr(settings, "trading_account", None),
        )
    except LookupError:
        pass
    scope, workspace, account = bootstrap_initial_scope(
        db,
        workspace_reference=getattr(settings, "trading_workspace", "legacy-local"),
    )
    config_path = default_config_path()
    snapshot = snapshot_env_file(config_path)
    try:
        update_env_file(
            config_path,
            {
                "TRADING_WORKSPACE": workspace.slug,
                "TRADING_ACCOUNT": str(account.id),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        restore_env_file(config_path, snapshot)
        raise
    get_settings.cache_clear()
    console.print(
        "[green]Created your first local trading workspace and Manual / journal "
        "account.[/green]"
    )
    return scope


def _configured_workspace(db):
    workspace = resolve_workspace(db, get_settings().trading_workspace)
    if workspace is None:
        raise LookupError("configured workspace was not found")
    return workspace


STARTER_PROMPTS = (
    "Show me today's economic news.",
    "Show me the previous six Core PCE releases.",
    "Review this chart: /absolute/path/to/chart.png",
    "Help me build an XAUUSD New York premarket plan.",
    "Size this trade: equity 10000, risk 0.5%, entry 2350, stop 2345, target 2365.",
    "Review my recent trades and identify patterns worth testing as an edge.",
    "Teach me the next lesson in my curriculum.",
)

OPTIONAL_HEALTH_LABELS = {
    "api_security": "remote API password (only needed when exposing the optional web API)",
    "web_search": "broad web search beyond your approved documentation domains",
}


@dataclass(frozen=True)
class GuidedChoice:
    key: str
    label: str
    description: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class OnboardingDefaults:
    timezone: str
    learning_mode: str
    markets: tuple[str, ...]
    sessions: tuple[str, ...]
    trading_style: str
    goals: tuple[str, ...]
    maximum_risk_percent: Decimal


ONBOARDING_RISK_CEILING_PERCENT = Decimal("5")


MODEL_PROVIDER_CHOICES = (
    GuidedChoice(
        "ollama",
        "Ollama (local)",
        "Runs on this computer with no per-request API charge.",
        ("local",),
    ),
    GuidedChoice(
        "openai",
        "OpenAI API",
        "Uses a separately billed OpenAI API key.",
        ("open ai", "gpt"),
    ),
    GuidedChoice(
        "anthropic",
        "Anthropic API",
        "Uses a separately billed Anthropic API key.",
        ("claude",),
    ),
)
DATABASE_CHOICES = (
    GuidedChoice(
        "local",
        "Local PostgreSQL",
        "Recommended for one trader on this computer.",
        ("postgres", "postgresql", "local postgres"),
    ),
    GuidedChoice(
        "neon",
        "Neon",
        "Hosted PostgreSQL; requires a private connection URL.",
        ("neon postgres",),
    ),
    GuidedChoice(
        "custom",
        "Custom PostgreSQL",
        "Use an existing PostgreSQL-compatible server.",
        ("other", "custom postgres"),
    ),
)
BROKER_CHOICES = (
    GuidedChoice(
        "none",
        "No broker yet",
        "Skip broker data now; this can be changed later.",
        ("no", "skip", "no broker", "disabled"),
    ),
    GuidedChoice(
        "oanda",
        "OANDA",
        "Read-only quotes, candles, account, positions, and fills.",
        ("oanda v20",),
    ),
    GuidedChoice(
        "metatrader",
        "MetaTrader 4 / 5",
        "Read-only live data and execution history through a terminal-side bridge.",
        ("mt4", "mt5", "meta trader", "metatrader 4", "metatrader 5"),
    ),
    GuidedChoice(
        "ibkr",
        "Interactive Brokers (planned)",
        "Planned read-only broker and account data adapter; not implemented yet.",
        ("ibkr", "interactive brokers", "interactive broker", "ib"),
    ),
    GuidedChoice(
        "alpaca",
        "Alpaca (planned)",
        "Planned stocks/ETFs/crypto market-data coverage; account feed is not implemented.",
        ("alpaca", "alpaca markets"),
    ),
    GuidedChoice(
        "twelve-data",
        "Twelve Data (planned)",
        "Planned unified FX/equities/indices data feed; not implemented yet.",
        ("twelve data", "twelve-data", "twelvedata"),
    ),
    GuidedChoice(
        "ctrader",
        "cTrader (planned)",
        "Planned OAuth account and market-data adapter for CFD and chart workflows.",
        ("cTrader", "ctrader", "c trader"),
    ),
)
METATRADER_PLATFORM_CHOICES = (
    GuidedChoice(
        "mt5",
        "MetaTrader 5",
        "Uses the included Windows companion service beside an official MT5 terminal.",
        ("5", "meta trader 5", "metatrader5"),
    ),
    GuidedChoice(
        "mt4",
        "MetaTrader 4",
        "Uses the documented read-only contract; a terminal-side bridge is required.",
        ("4", "meta trader 4", "metatrader4"),
    ),
)
NEWS_CHOICES = (
    GuidedChoice(
        "none",
        "No news provider yet",
        "Skip calendar/news data now; this can be changed later.",
        ("no", "skip", "no news", "disabled"),
    ),
    GuidedChoice(
        "forex-factory",
        "Forex Factory",
        "Free read-only weekly economic calendar; no API key required.",
        ("forex factory", "forexfactory", "ff"),
    ),
    GuidedChoice(
        "trading-economics",
        "Trading Economics",
        "Economic calendar and timestamped news metadata.",
        ("trading economics", "trading_economics", "te"),
    ),
)
TRADINGVIEW_CHOICES = (
    GuidedChoice(
        "disabled",
        "TradingView alerts disabled",
        "Skip inbound chart alerts; this remains the safe default.",
        ("none", "no", "skip", "off"),
    ),
    GuidedChoice(
        "enabled",
        "TradingView alerts enabled",
        "Receive alerts only after a public HTTPS proxy verifies TradingView "
        "mTLS and source IPs.",
        ("yes", "on", "webhook", "tradingview"),
    ),
)
EXPERIENCE_CHOICES = (
    GuidedChoice(
        "beginner",
        "Beginner",
        "Newer to trading concepts; recommend foundations and guided explanations.",
        ("new", "novice"),
    ),
    GuidedChoice(
        "intermediate",
        "Intermediate",
        "Comfortable with trading terminology but still developing a process.",
        ("medium",),
    ),
    GuidedChoice(
        "advanced",
        "Advanced",
        "Already fluent in trading terminology; learning remains fully available on demand.",
        ("experienced", "expert"),
    ),
)
LEARNING_MODE_CHOICES = (
    GuidedChoice(
        "guided",
        "Guided curriculum",
        "Walk through lessons in order and suggest the next learning step.",
        ("course", "guided learning"),
    ),
    GuidedChoice(
        "flexible",
        "Flexible curriculum",
        "Keep a recommended path, but choose lessons in any order.",
        ("balanced", "self paced", "self-paced"),
    ),
    GuidedChoice(
        "on_demand",
        "On-demand teaching",
        "Do not prompt lessons; teach and answer questions whenever requested.",
        ("on demand", "questions", "ask anytime"),
    ),
    GuidedChoice(
        "disabled",
        "Not now",
        "Pause curriculum suggestions; natural-language questions still work.",
        ("none", "off", "skip", "no"),
    ),
)
ACCOUNT_TYPE_CHOICES = (
    GuidedChoice(
        "not_configured",
        "Not configured yet",
        "Skip account limits for now; pre-trade checks will state that no "
        "account rules are loaded.",
        ("none", "skip", "later"),
    ),
    GuidedChoice(
        "personal",
        "Personal or demo account",
        "Your own capital or a practice account; detailed rules are optional.",
        ("personal trading", "own account", "retail", "demo", "practice", "paper"),
    ),
    GuidedChoice(
        "prop",
        "Prop firm account",
        "An evaluation, verification, or funded account with firm rules.",
        ("prop trading", "funded", "challenge"),
    ),
)
PROP_PHASE_CHOICES = (
    GuidedChoice(
        "evaluation",
        "Evaluation / challenge",
        "The initial profit-target and drawdown stage.",
        ("challenge", "phase 1", "evaluation phase"),
    ),
    GuidedChoice(
        "verification",
        "Verification",
        "A second qualification stage before funding.",
        ("phase 2", "verification phase"),
    ),
    GuidedChoice(
        "funded",
        "Funded",
        "A funded or performance account after qualification.",
        ("performance", "funded account"),
    ),
)
ACCOUNT_RULE_POLICY_CHOICES = (
    GuidedChoice("unknown", "Unknown", "Record that this rule still needs verification."),
    GuidedChoice("allowed", "Allowed", "The program permits this activity."),
    GuidedChoice("prohibited", "Prohibited", "The program forbids this activity."),
    GuidedChoice(
        "restricted",
        "Restricted",
        "The activity is allowed only under additional conditions.",
    ),
)
DRAWDOWN_TYPE_CHOICES = (
    GuidedChoice("unknown", "Unknown", "Record that the drawdown calculation needs verification."),
    GuidedChoice("static", "Static", "The loss floor stays fixed."),
    GuidedChoice(
        "balance_based",
        "Balance based",
        "The limit is calculated from account balance.",
        ("balance",),
    ),
    GuidedChoice(
        "equity_based",
        "Equity based",
        "The limit includes unrealized equity.",
        ("equity",),
    ),
    GuidedChoice(
        "trailing",
        "Trailing",
        "The loss floor can move as the account reaches new highs.",
    ),
)
ONBOARDING_EDIT_CHOICES = (
    GuidedChoice("display_name", "Display name", "Change how the agent addresses you."),
    GuidedChoice("timezone", "Timezone", "Change session and news time labeling."),
    GuidedChoice("experience", "Experience level", "Change explanation depth."),
    GuidedChoice("learning_mode", "Teaching mode", "Change curriculum guidance."),
    GuidedChoice("learning_topics", "Learning topics", "Change curriculum subjects."),
    GuidedChoice("markets", "Markets and instruments", "Change followed symbols."),
    GuidedChoice("sessions", "Trading sessions", "Change preferred sessions."),
    GuidedChoice("trading_style", "Trading style", "Change descriptive context."),
    GuidedChoice("goals", "Goals", "Change trading and process goals."),
    GuidedChoice("risk", "Maximum risk", "Change the profile risk preference."),
    GuidedChoice(
        "account",
        "Account and prop rules",
        "Change personal/prop classification, size, phase, and restrictions.",
        ("prop rules", "challenge rules", "account rules"),
    ),
    GuidedChoice("broker", "Broker", "Change the read-only broker provider."),
    GuidedChoice("news", "News/calendar", "Change the news provider."),
    GuidedChoice(
        "tradingview",
        "TradingView alerts",
        "Enable or disable verified inbound chart alerts.",
        ("chart alerts", "webhook"),
    ),
    GuidedChoice(
        "discard",
        "Discard onboarding",
        "Exit only after a separate confirmation; no values are saved.",
        ("exit", "cancel"),
    ),
)

MARKET_ALIASES = {
    "gold": "XAUUSD",
    "xau/usd": "XAUUSD",
    "s&p500": "SPX500",
    "s&p 500": "SPX500",
    "sp500": "SPX500",
    "nasdaq": "NAS100",
    "nasdaq100": "NAS100",
    "nasdaq 100": "NAS100",
}
MARKET_DEFAULT_REQUESTS = frozenset(
    {
        "any",
        "choose for me",
        "choose one",
        "choose one for me",
        "i do not know",
        "i dont know",
        "no idea",
        "no preference",
        "not sure",
        "pick for me",
        "pick one",
        "pick one for me",
        "recommend one",
        "whatever",
        "you choose",
        "you pick",
    }
)
COMMON_MARKETS = (
    "XAUUSD",
    "XAGUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
    "SPX500",
    "NAS100",
    "US30",
    "BTCUSD",
    "ETHUSD",
)
SESSION_ALIASES = {
    "newyork": "New York",
    "new york": "New York",
    "ny": "New York",
    "nyc": "New York",
    "london": "London",
    "ldn": "London",
    "asian": "Asia",
    "asia": "Asia",
    "tokyo": "Asia",
    "sydney": "Sydney",
}
TRADING_STYLE_SPELLING = {
    "acumulation": "accumulation",
    "accumalation": "accumulation",
    "distribuition": "distribution",
    "imbalanace": "imbalance",
    "liquidty": "liquidity",
    "manuplation": "manipulation",
    "retext": "retest",
    "stradegy": "strategy",
    "stratedgy": "strategy",
    "timefram": "timeframe",
    "wycoff": "Wyckoff",
}
COMMON_GOALS = (
    "consistency",
    "profitability",
    "psychology",
    "process discipline",
    "risk management",
    "measurable edge",
)

_direct_command_audits: list[tuple[uuid.UUID, RequestScope]] = []


@contextmanager
def _direct_command_audit_lifecycle():
    """Finish every confirmed direct mutation when the CLI invocation exits."""
    _direct_command_audits.clear()
    error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        pending = tuple(_direct_command_audits)
        _direct_command_audits.clear()
        for audit_id, scope in pending:
            with SessionLocal() as audit_db:
                complete_mutation_audit(
                    audit_db,
                    audit_id,
                    scope=scope,
                    error=error,
                )


def _finish_pending_direct_audits() -> None:
    """A later confirmation proves earlier sequential mutations returned."""
    pending = tuple(_direct_command_audits)
    _direct_command_audits.clear()
    for audit_id, scope in pending:
        with SessionLocal() as audit_db:
            complete_mutation_audit(audit_db, audit_id, scope=scope)


@lru_cache
def _runtime_policy() -> PolicyEngine:
    return PolicyEngine.load()


def _authorize_direct(
    name: str,
    arguments: dict,
    *,
    mutating: bool = False,
    deterministic: bool = False,
    assume_yes: bool = False,
) -> None:
    policy = _runtime_policy()
    context = ToolContext(
        name=name,
        arguments=arguments,
        mutating=mutating,
        deterministic=deterministic,
    )
    policy.authorize_registered_action(context)
    hooks = ExecutionHooks(
        policy,
        lambda action, values: assume_yes or _confirm_agent_mutation(action, values),
    )
    hooks.before_execute(context)
    if mutating:
        _finish_pending_direct_audits()
        # The audit table must exist before the durable confirmation is inserted.
        upgrade_database()
        with SessionLocal() as db:
            try:
                scope = _current_scope(db)
            except LookupError:
                # Fresh bootstrap/onboarding has no account to own an audit row yet.
                return
            audit = record_direct_cli_confirmation(
                db,
                scope=scope,
                action=name,
                arguments=arguments,
            )
            _direct_command_audits.append((audit.id, scope))


def _print_model(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    console.print_json(data=jsonable_encoder(value))


def _render_broker_setup_error(
    settings: Settings,
    error: Exception,
    *,
    intended_provider: str | None = None,
) -> None:
    provider = intended_provider or settings.broker_provider
    config_path = default_config_path().expanduser().resolve()
    console.print()
    console.print("[bold red]Broker setup is incomplete[/bold red]")
    console.print(f"[red]{escape_markup(str(error))}[/red]")
    console.print()
    console.print(f"[dim]Configuration file:[/dim] {escape_markup(str(config_path))}")
    if provider == "metatrader":
        console.print(
            "MetaTrader requires a separate read-only bridge running beside the MT5 "
            "terminal on Windows or a VPS."
        )
        console.print(
            "Set [cyan]BROKER_PROVIDER=metatrader[/cyan], "
            "[cyan]METATRADER_BRIDGE_URL[/cyan], "
            "[cyan]METATRADER_BRIDGE_TOKEN[/cyan], and "
            "[cyan]METATRADER_ACCOUNT_ID[/cyan] in that file."
        )
        console.print(
            "The bridge token is a dedicated 32+ character secret you generate; "
            "it is not your MetaTrader password."
        )
        console.print(
            "Then start the bridge and run "
            "[cyan]trade broker configure-metatrader --label NAME[/cyan]."
        )
    elif provider in {"ibkr", "alpaca", "twelve-data", "ctrader"}:
        display_name = (
            "Interactive Brokers"
            if provider == "ibkr"
            else "Alpaca"
            if provider == "alpaca"
            else "Twelve Data"
            if provider == "twelve-data"
            else "cTrader"
        )
        console.print(
            f"{display_name} is marked as planned in the public roadmap and is not yet "
            "runnable in this release."
        )
        console.print(
            "It currently appears in planning views; choose a live data source now for "
            "broker reads."
        )
    elif provider == "oanda":
        console.print(
            "Set [cyan]BROKER_PROVIDER=oanda[/cyan], "
            "[cyan]OANDA_API_TOKEN[/cyan], and "
            "[cyan]OANDA_ACCOUNT_ID[/cyan] in that file."
        )
        console.print(
            "Then run [cyan]trade broker configure-oanda --label NAME[/cyan]."
        )
    else:
        console.print(
            "Choose [cyan]BROKER_PROVIDER=oanda[/cyan] or "
            "[cyan]BROKER_PROVIDER=metatrader[/cyan] in that file, or run "
            "[cyan]trade setup[/cyan] for guided configuration."
        )
    console.print("[dim]Nothing was changed.[/dim]")


def _render_broker_request_error(
    settings: Settings,
    error: Exception,
    *,
    operation: str,
) -> None:
    provider_name = {
        "metatrader": "MetaTrader bridge",
        "oanda": "OANDA",
        "ibkr": "Interactive Brokers",
        "alpaca": "Alpaca",
        "twelve-data": "Twelve Data",
        "ctrader": "cTrader",
    }.get(settings.broker_provider, settings.broker_provider.title())
    console.print()
    console.print(f"[bold red]{provider_name} {operation} failed[/bold red]")
    console.print(f"[red]{escape_markup(str(error))}[/red]")
    if settings.broker_provider == "metatrader":
        console.print(
            "Confirm the MT5 terminal and read-only bridge are running, the bridge URL "
            "is reachable, and the token and account ID match on both machines."
        )
    else:
        console.print(
            "Confirm the configured environment, account ID, API token, and network "
            "connection."
        )
    console.print(
        "Run [cyan]trade integrations --verify-live[/cyan] for bounded read-only checks."
    )
    console.print("[dim]Nothing was changed.[/dim]")


def _render_cancelled_mutation() -> None:
    console.print("[yellow]Cancelled. Nothing was changed.[/yellow]")


def _render_health(report: HealthReport) -> None:
    table = Table(title="Trading Agent health", show_header=True)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    colors = {"ok": "green", "warning": "yellow", "error": "red"}
    for check in report.checks:
        color = colors[check.status]
        table.add_row(check.name, f"[{color}]{check.status}[/{color}]", check.detail)
    console.print(table)


def _render_startup_health(report: HealthReport) -> None:
    if not report.ready:
        _render_health(report)
        return
    healthy = sum(check.status == "ok" for check in report.checks)
    optional = [check.name for check in report.checks if check.status == "warning"]
    console.print(f"[green]✓ Ready[/green] · {healthy} checks passed")
    if optional:
        labels = [OPTIONAL_HEALTH_LABELS.get(name, name.replace("_", " ")) for name in optional]
        console.print(
            "[dim]Optional features not configured: "
            f"{'; '.join(labels)}. Run `/health` for setup instructions.[/dim]"
        )


def _render_starter_prompts() -> None:
    console.print("[bold]Try asking:[/bold]")
    for prompt in STARTER_PROMPTS:
        console.print(f"  [cyan]›[/cyan] {prompt}")


def _prompt_startup_action() -> str | None:
    if not sys.stdin.isatty():
        return None

    options = (
        ("1", "Do a day-start check (news + open plans)."),
        ("2", "Show account and broker readiness for the selected symbol."),
        ("3", "Start a new trade plan with guided defaults."),
    )
    console.print()
    console.print("[bold]Quick start[/bold]")
    for key, label in options:
        console.print(f"  [cyan]{key}[/cyan]  {label}")
    console.print(
        "  [cyan]Enter[/cyan]  Continue with an empty prompt and type naturally"
    )
    while True:
        selection = console.input("[dim]Quick start choice (1-3 or Enter):[/dim] ").strip()
        if not selection:
            return None
        if selection == "1":
            return "Show me today's economic news and an operational day-start summary."
        if selection == "2":
            return (
                "Show my live broker status and whether I'm ready to size a trade now."
            )
        if selection == "3":
            return "Help me build a New York premarket plan for XAUUSD."
        console.print("[yellow]Please enter 1, 2, 3, or press Enter to continue.[/yellow]")


def _literal_terminal_text(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    return "".join(
        character
        for character in normalized
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf", "Zl", "Zp"}
    )


def _render_startup_memory(memory: StartupMemory, *, detailed: bool = False) -> None:
    console.print()
    console.print(Text("Recall", style="bold"))
    if not memory.has_content:
        console.print(
            Text(
                "  Nothing recorded in this strategy scope yet. New sessions, plans, "
                "reflections, and mindset check-ins will appear here.",
                style="dim",
            )
        )
        return

    if memory.goals:
        goals = memory.goals if detailed else memory.goals[:3]
        console.print(Text("  Goals", style="cyan"))
        for goal in goals:
            console.print(Text(_literal_terminal_text(f"    • {goal}")))
        if not detailed and len(memory.goals) > len(goals):
            console.print(Text(f"    + {len(memory.goals) - len(goals)} more", style="dim"))

    if memory.account:
        account = memory.account
        console.print(Text("  Active account", style="cyan"))
        account_label = (
            f"    {account.name} · {account.account_type} · {account.phase} · "
            f"{account.currency} {account.account_size}"
        )
        console.print(Text(_literal_terminal_text(account_label)))
        if account.firm_name:
            firm = account.firm_name
            if account.program_name:
                firm += f" · {account.program_name}"
            console.print(Text(_literal_terminal_text(f"    {firm}"), style="dim"))
        reminders = (
            account.reminders if detailed else account.reminders[:3]
        )
        for reminder in reminders:
            console.print(Text(_literal_terminal_text(f"    • {reminder}")))
        if not detailed and len(account.reminders) > len(reminders):
            console.print(
                Text(
                    f"    + {len(account.reminders) - len(reminders)} more rules",
                    style="dim",
                )
            )
        if not account.reminders:
            console.print(
                Text("    No loss limits or restrictions recorded.", style="yellow")
            )

    if memory.strategy:
        strategy = memory.strategy
        console.print(Text("  Strategy", style="cyan"))
        label = f"    {strategy.name} v{strategy.version}"
        if detailed:
            label += f" · sha256 {strategy.content_hash[:12]}"
        console.print(Text(_literal_terminal_text(label)))

    if memory.prior_session:
        prior = memory.prior_session
        console.print(Text("  Previous session", style="cyan"))
        console.print(
            Text(
                _literal_terminal_text(
                    f"    {prior.name} · {prior.turn_count} turns · "
                    f"{prior.last_activity_at}"
                )
            )
        )
        if detailed and prior.title:
            console.print(
                Text(_literal_terminal_text(f"    {prior.title}"), style="dim")
            )

    plans = memory.open_plans if detailed else memory.open_plans[:2]
    if plans:
        console.print(Text("  Carry-over plans", style="cyan"))
        for plan in plans:
            console.print(
                Text(
                    _literal_terminal_text(
                        f"    {plan.reference} · {plan.instrument} {plan.direction} · "
                        f"{plan.status} · {plan.setup_name}"
                    )
                )
            )
        if not detailed and len(memory.open_plans) > len(plans):
            console.print(
                Text(f"    + {len(memory.open_plans) - len(plans)} more", style="dim")
            )

    reflections = (
        memory.recent_reflections if detailed else memory.recent_reflections[:1]
    )
    if reflections:
        console.print(Text("  Recent review", style="cyan"))
        for reflection in reflections:
            process = (
                f" · process {reflection.process_score}"
                if reflection.process_score is not None
                else ""
            )
            console.print(
                Text(
                    _literal_terminal_text(
                        f"    {reflection.plan_reference} · {reflection.realized_r}R · "
                        f"execution {reflection.execution_grade}{process}"
                    )
                )
            )

    mindset_items = memory.recent_mindset if detailed else memory.recent_mindset[:1]
    if mindset_items:
        console.print(Text("  Recent mindset", style="cyan"))
        for item in mindset_items:
            tags = f" · {', '.join(item.emotion_tags)}" if item.emotion_tags else ""
            risk = "risk accepted" if item.accepted_risk else "risk not accepted"
            console.print(
                Text(
                    _literal_terminal_text(
                        f"    {item.phase.replace('_', ' ')} · readiness "
                        f"{item.readiness}/5 · {risk}{tags}"
                    )
                )
            )

    if not detailed:
        console.print(Text("  /memory shows the full bounded recall set.", style="dim"))


def _startup_memory_references(memory: StartupMemory) -> list[UsedReference]:
    return [
        UsedReference(
            kind=reference.kind,
            label=reference.label,
            locator=reference.locator,
            retrieved_at=reference.retrieved_at,
        )
        for reference in memory.references
    ]


def _render_integrations() -> None:
    table = Table(title="Data integrations", show_header=True)
    table.add_column("Type")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("What it provides")
    for option in integration_options():
        color = {
            "ready": "green",
            "adapter-only": "yellow",
            "planned": "dim",
        }[option.status]
        table.add_row(
            option.kind,
            option.name,
            f"[{color}]{'implemented' if option.status == 'ready' else option.status}[/{color}]",
            option.capability,
        )
    console.print(table)
    console.print(
        "[dim]Implemented means the adapter code exists. It does not mean credentials "
        "are configured or that a live provider has been verified.[/dim]"
    )


def _render_integration_verifications(
    reports: tuple[IntegrationVerification, ...],
) -> None:
    console.print()
    colors = {
        "implemented": "green",
        "planned": "dim",
        "configured": "green",
        "incomplete": "yellow",
        "not configured": "dim",
        "not applicable": "dim",
        "verified now": "green",
        "verified previously": "green",
        "not tested": "yellow",
        "inbound only": "yellow",
        "unavailable": "red",
        "observed": "green",
        "not observed": "yellow",
    }
    console.print(Text("Integration qualification", style="bold"))
    console.print(
        Text(
            "Code availability, setup, connection testing, and real evidence are "
            "reported separately.",
            style="dim",
        )
    )
    console.print()
    for report in reports:
        last_success = (
            report.last_success_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            if report.last_success_at
            else "never"
        )
        console.print(Text(report.name, style="bold cyan"))
        for label, value in (
            ("Code", report.implementation),
            ("Setup", report.configuration),
            ("Connection", report.reachability),
            ("Evidence", report.evidence),
        ):
            line = Text(f"  {label:<12}", style="dim")
            line.append(value, style=colors[value])
            console.print(line)
        console.print(Text(f"  {report.detail}"))
        console.print(Text(f"  Last success: {last_success}", style="dim"))
        if report.next_action:
            console.print(Text(f"  Next: {report.next_action}", style="yellow"))
        console.print()
    console.print(
        Text(
            "A live verification is a bounded read-only check. It does not import "
            "records, stream ticks, or place/change orders.",
            style="dim",
        )
    )


def _choice_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _resolve_guided_choice(
    value: str,
    choices: tuple[GuidedChoice, ...],
) -> str | None:
    token = _choice_token(value)
    for index, choice in enumerate(choices, start=1):
        accepted = (choice.key, choice.label, *choice.aliases, str(index))
        if token in {_choice_token(item) for item in accepted}:
            return choice.key
    return None


def _choice_suggestion(
    value: str,
    choices: tuple[GuidedChoice, ...],
) -> str | None:
    labels: dict[str, str] = {}
    for choice in choices:
        for candidate in (choice.key, choice.label, *choice.aliases):
            labels[_choice_token(candidate)] = choice.label
    match = difflib.get_close_matches(
        _choice_token(value),
        labels,
        n=1,
        cutoff=0.6,
    )
    return labels[match[0]] if match else None


def _prompt_guided_choice(
    heading: str,
    choices: tuple[GuidedChoice, ...],
    *,
    default: str,
) -> str:
    console.print(f"\n[bold]{heading}[/bold]")
    for index, choice in enumerate(choices, start=1):
        marker = " [green](current/default)[/green]" if choice.key == default else ""
        console.print(
            f"  [cyan]{index}.[/cyan] [bold]{choice.label}[/bold]{marker}\n"
            f"     [dim]{choice.description}[/dim]"
        )
    default_choice = next(choice for choice in choices if choice.key == default)
    while True:
        raw = typer.prompt(
            "Choose by number or name",
            default=default_choice.label,
        )
        resolved = _resolve_guided_choice(raw, choices)
        if resolved is not None:
            selected = next(choice for choice in choices if choice.key == resolved)
            console.print(f"[green]✓ Selected {selected.label}.[/green]")
            return resolved
        suggestion = _choice_suggestion(raw, choices)
        detail = f" Did you mean {suggestion}?" if suggestion else ""
        valid = ", ".join(
            f"{index} ({choice.label})" for index, choice in enumerate(choices, start=1)
        )
        console.print(
            f"[yellow]I did not recognize that entry.{detail} "
            f"Enter {valid}, or press Enter for {default_choice.label}.[/yellow]"
        )


def _resolve_cli_choice(
    value: str,
    choices: tuple[GuidedChoice, ...],
    *,
    option_name: str,
) -> str:
    resolved = _resolve_guided_choice(value, choices)
    if resolved is not None:
        return resolved
    suggestion = _choice_suggestion(value, choices)
    valid = ", ".join(choice.key for choice in choices)
    detail = f" Did you mean “{suggestion}”?" if suggestion else ""
    raise ValueError(f"unrecognized {option_name}.{detail} Valid choices: {valid}")


@lru_cache
def _timezone_names() -> dict[str, str]:
    return {name.casefold(): name for name in available_timezones()}


def _normalize_timezone(value: str) -> str | None:
    aliases = {
        "eastern": "America/New_York",
        "est": "America/New_York",
        "edt": "America/New_York",
        "new york": "America/New_York",
        "central": "America/Chicago",
        "cst": "America/Chicago",
        "chicago": "America/Chicago",
        "mountain": "America/Denver",
        "denver": "America/Denver",
        "pacific": "America/Los_Angeles",
        "pst": "America/Los_Angeles",
        "los angeles": "America/Los_Angeles",
        "utc": "UTC",
        "gmt": "UTC",
    }
    stripped = value.strip()
    candidate = aliases.get(stripped.casefold(), stripped)
    canonical = _timezone_names().get(candidate.casefold())
    if canonical is None:
        return None
    try:
        ZoneInfo(canonical)
    except ZoneInfoNotFoundError:
        return None
    return canonical


def _local_timezone_name() -> str:
    local = datetime.now().astimezone().tzinfo
    candidates = (
        getattr(local, "key", None),
        str(local) if local is not None else None,
        "UTC",
    )
    for candidate in candidates:
        if candidate and (normalized := _normalize_timezone(candidate)):
            return normalized
    return "UTC"


def _profile_timezone(db, scope: RequestScope) -> ZoneInfo:
    profile = get_trader_profile(db, scope=scope)
    timezone_name = profile.timezone if profile is not None else _local_timezone_name()
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _format_profile_datetime(value: datetime, timezone: ZoneInfo) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(timezone).strftime("%Y-%m-%d %H:%M %Z")


def _prompt_timezone(default: str) -> str:
    normalized_default = _normalize_timezone(default)
    if normalized_default is None:
        normalized_default = _local_timezone_name()
        console.print(
            "[yellow]The previously saved timezone is invalid. "
            f"I replaced the default with this computer's timezone: "
            f"{normalized_default}.[/yellow]"
        )
    console.print(
        "\n[bold]Timezone[/bold]\n"
        "[dim]Used to label sessions and news correctly. Enter a city timezone such as "
        "America/New_York, or a familiar name such as Eastern.[/dim]"
    )
    while True:
        raw = typer.prompt("Timezone", default=normalized_default)
        normalized = _normalize_timezone(raw)
        if normalized is not None:
            if normalized != raw:
                console.print(f"[green]✓ Using {normalized}.[/green]")
            return normalized
        suggestion = difflib.get_close_matches(
            raw.casefold(),
            _timezone_names(),
            n=1,
            cutoff=0.6,
        )
        detail = f" Did you mean {_timezone_names()[suggestion[0]]}?" if suggestion else ""
        console.print(
            f"[yellow]That is not a recognized timezone.{detail} "
            f"Press Enter to keep {normalized_default}.[/yellow]"
        )


def _prompt_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _prompt_bounded_text(
    label: str,
    *,
    default: str,
    maximum_length: int,
    field_name: str,
) -> str:
    while True:
        value = typer.prompt(label, default=default).strip()
        if not value:
            console.print(
                f"[yellow]{field_name} cannot be empty. Press Enter to use the "
                "displayed default.[/yellow]"
            )
            continue
        if len(value) > maximum_length:
            console.print(
                f"[yellow]{field_name} is {len(value):,} characters; the limit is "
                f"{maximum_length:,}. Please shorten it.[/yellow]"
            )
            continue
        try:
            value = validate_profile_text(
                value,
                field_name=field_name,
                maximum_length=maximum_length,
            )
        except ValueError as exc:
            console.print(f"[yellow]{exc}. Please try again.[/yellow]")
            continue
        return value


def _normalize_market(value: str) -> str:
    stripped = value.strip()
    alias = MARKET_ALIASES.get(stripped.casefold())
    return alias or stripped.upper()


def _requests_default_market(value: str) -> bool:
    folded = value.casefold().replace("'", "").replace("’", "")
    normalized = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    return normalized in MARKET_DEFAULT_REQUESTS


def _prompt_markets(default: list[str]) -> list[str]:
    console.print(
        "\n[bold]Markets and instruments[/bold]\n"
        "[dim]Separate symbols with commas. Examples: XAUUSD, NAS100, EURUSD.[/dim]"
    )
    while True:
        raw_response = typer.prompt(
            "Markets/instruments",
            default=",".join(default),
        )
        if _requests_default_market(raw_response):
            selected = _normalize_market(default[0] if default else "XAUUSD")
            console.print(
                f"[cyan]No problem—using {selected} as your starter market. "
                "This only personalizes the agent; it does not enable or place trades.[/cyan]"
            )
            console.print(f"[green]✓ Markets: {selected}[/green]")
            return [selected]
        raw_values = _prompt_values(raw_response)
        if not raw_values:
            console.print(
                "[yellow]Enter at least one market or press Enter for the default.[/yellow]"
            )
            continue
        normalized: list[str] = []
        retry = False
        for raw in raw_values:
            market = _normalize_market(raw)
            if not re.fullmatch(r"[A-Z0-9._:/-]{2,32}", market):
                if re.search(r"\s", raw.strip()):
                    console.print(
                        "[yellow]I read that as a sentence, not a broker symbol. "
                        "Enter symbols such as XAUUSD or NAS100, or say "
                        "\"choose one for me\" to use the displayed starter market.[/yellow]"
                    )
                    retry = True
                    break
                console.print(
                    "[yellow]That entry does not look like a market symbol. "
                    "Use letters/numbers such as XAUUSD or NAS100.[/yellow]"
                )
                retry = True
                break
            suggestion = difflib.get_close_matches(
                market,
                COMMON_MARKETS,
                n=1,
                cutoff=0.78,
            )
            if suggestion and suggestion[0] != market:
                proposed = suggestion[0]
                if typer.confirm(
                    f"That symbol is close to {proposed}. Use {proposed}?",
                    default=True,
                ):
                    market = proposed
                else:
                    console.print(
                        f"[yellow]Keeping {market} as a custom symbol. "
                        "You can change it later with `trade onboard`.[/yellow]"
                    )
            if market not in normalized:
                normalized.append(market)
        if retry:
            continue
        console.print(f"[green]✓ Markets: {', '.join(normalized)}[/green]")
        return normalized


def _normalize_sessions(values: list[str]) -> list[str]:
    sessions: list[str] = []
    for value in values:
        stripped = value.strip()
        normalized = SESSION_ALIASES.get(stripped.casefold(), stripped)
        if normalized not in sessions:
            sessions.append(normalized)
    return sessions


def _prompt_sessions(default: list[str]) -> list[str]:
    console.print(
        "\n[bold]Trading sessions[/bold]\n"
        "[dim]Examples: New York, London, Asia. Separate multiple sessions with commas.[/dim]"
    )
    while True:
        raw_sessions = _prompt_values(typer.prompt("Trading sessions", default=",".join(default)))
        reviewed: list[str] = []
        invalid = False
        for raw in raw_sessions:
            normalized = SESSION_ALIASES.get(raw.strip().casefold())
            if normalized is None:
                suggestion = difflib.get_close_matches(
                    raw.strip().casefold(),
                    SESSION_ALIASES,
                    n=1,
                    cutoff=0.68,
                )
                if suggestion:
                    proposed = SESSION_ALIASES[suggestion[0]]
                    if typer.confirm(
                        f"That session is close to {proposed}. Use {proposed}?",
                        default=True,
                    ):
                        normalized = proposed
                if normalized is None:
                    try:
                        normalized = validate_profile_text(
                            raw,
                            field_name="Trading session",
                            maximum_length=48,
                        )
                    except ValueError as exc:
                        console.print(f"[yellow]Trading session was not accepted: {exc}.[/yellow]")
                        invalid = True
                        continue
                    console.print(
                        f"[yellow]“{escape_markup(raw)}” is not a standard session name. "
                        "Keeping it as a custom session.[/yellow]"
                    )
            reviewed.append(normalized)
        if invalid:
            console.print(
                "[yellow]Try again with session names such as New York, London, "
                "Asia, or a relevant custom session label.[/yellow]"
            )
            continue
        if len(reviewed) > 12:
            console.print("[yellow]Choose no more than 12 trading sessions.[/yellow]")
            continue
        if len({value.casefold() for value in reviewed}) != len(reviewed):
            console.print("[yellow]Trading sessions cannot contain duplicates.[/yellow]")
            continue
        if sum(len(value) for value in reviewed) > 384:
            console.print(
                "[yellow]Trading session labels exceed the combined 384-character limit.[/yellow]"
            )
            continue
        sessions = _normalize_sessions(reviewed)
        if sessions:
            console.print(f"[green]✓ Sessions: {escape_markup(', '.join(sessions))}[/green]")
            return sessions
        console.print("[yellow]Enter at least one session or press Enter for the default.[/yellow]")


def _prompt_goal_item(goal: str, accepted: list[str]) -> str | None:
    """Review one goal while retaining every previously accepted item."""
    while True:
        remaining_characters = 2_000 - sum(len(item) for item in accepted)
        maximum_length = min(160, remaining_characters)
        if maximum_length < 1:
            console.print(
                "[yellow]The accepted goals already use the combined 2,000-character "
                "limit, so the remaining item was not added. Previously accepted "
                "goals are unchanged.[/yellow]"
            )
            return None
        try:
            goal = validate_profile_text(
                goal,
                field_name="Goal",
                maximum_length=maximum_length,
            )
        except ValueError as exc:
            console.print(f"[yellow]Goal was not accepted: {exc}.[/yellow]")
            console.print(
                "[dim]Previously accepted goals are unchanged. Enter one replacement "
                "related to trading, risk, learning, or process.[/dim]"
            )
            goal = typer.prompt("Replacement goal")
            continue

        suggestion = difflib.get_close_matches(
        goal.casefold(),
        COMMON_GOALS,
        n=1,
        cutoff=0.84,
        )
        if suggestion and suggestion[0].casefold() != goal.casefold():
            proposed = suggestion[0]
            if typer.confirm(
                f"That goal is close to “{proposed}”. Use “{proposed}”?",
                default=True,
            ):
                goal = proposed
        try:
            goal = validate_profile_text(
                goal,
                field_name="Goal",
                require_trading_goal=True,
                maximum_length=maximum_length,
            )
        except ValueError as exc:
            console.print(f"[yellow]Goal was not accepted: {exc}.[/yellow]")
            console.print(
                "[dim]Previously accepted goals are unchanged. Examples: consistency, "
                "risk discipline, journal every trade.[/dim]"
            )
            goal = typer.prompt("Replacement goal")
            continue
        if goal.casefold() in {item.casefold() for item in accepted}:
            console.print(
                "[yellow]That goal duplicates one already accepted. "
                "Previously accepted goals are unchanged.[/yellow]"
            )
            goal = typer.prompt("Replacement goal")
            continue
        return goal


def _prompt_goals(default: list[str]) -> list[str]:
    console.print(
        "\n[bold]Goals[/bold]\n"
        "[dim]These help the agent emphasize process and review. Separate goals with "
        "commas. Examples: consistency, measurable edge, process discipline.[/dim]"
    )
    while True:
        goals = _prompt_values(typer.prompt("Goals", default=",".join(default)))
        if not goals:
            console.print(
                "[yellow]Enter at least one goal or press Enter for the default.[/yellow]"
            )
            continue
        if len(goals) > 20:
            console.print("[yellow]Choose no more than 20 goals.[/yellow]")
            continue
        corrected: list[str] = []
        for goal in goals:
            reviewed = _prompt_goal_item(goal, corrected)
            if reviewed is not None:
                corrected.append(reviewed)
        console.print(f"[green]✓ Goals: {escape_markup(', '.join(corrected))}[/green]")
        return corrected


def _review_trading_style_spelling(value: str) -> str:
    reviewed = value
    for match in tuple(re.finditer(r"\b[\w'-]+\b", value, re.UNICODE)):
        replacement = TRADING_STYLE_SPELLING.get(match.group(0).casefold())
        if replacement is None:
            continue
        if match.group(0)[:1].isupper() and replacement[:1].islower():
            replacement = replacement.capitalize()
        if typer.confirm(
            f"Possible wording typo: “{match.group(0)}” → “{replacement}”. "
            "This checks spelling only, not whether the trading idea is correct. "
            "Use the suggested wording?",
            default=True,
        ):
            reviewed = re.sub(
                rf"\b{re.escape(match.group(0))}\b",
                replacement,
                reviewed,
                count=1,
                flags=re.IGNORECASE,
            )
    if reviewed != value:
        console.print(f"[green]✓ Trading style spelling: {escape_markup(reviewed)}[/green]")
    return reviewed


def _prompt_trading_style(default: str) -> str:
    console.print(
        "\n[bold]Trading style[/bold]\n"
        "[dim]Describe how you currently read and execute trades. This is context, "
        "not a permanent strategy rule; isolated strategy rules are created later. "
        "Examples: break and retest; Wyckoff accumulation with lower-timeframe "
        "confirmation; discretionary trend following.[/dim]"
    )
    value = _prompt_bounded_text(
        "Describe your trading style",
        default=default,
        maximum_length=4_000,
        field_name="Trading style",
    )
    return _review_trading_style_spelling(value)


def _prompt_learning_topics(default: list[str]) -> list[str]:
    topic_keys = list(all_learning_topics())
    console.print(
        "\n[bold]Learning topics[/bold]\n"
        "[dim]Choose several by number or name, separated with commas. Enter `all` "
        "to include the full learning library.[/dim]"
    )
    for index, key in enumerate(topic_keys, start=1):
        marker = " [green](recommended)[/green]" if key in default else ""
        console.print(f"  [cyan]{index}.[/cyan] {TOPIC_LABELS[key]} [dim]({key})[/dim]{marker}")
    while True:
        raw = typer.prompt(
            "Topics",
            default=",".join(default),
        )
        values = _prompt_values(raw)
        if any(value.strip().casefold() == "all" for value in values):
            selected = topic_keys
        else:
            selected = []
            invalid = []
            for value in values:
                stripped = value.strip()
                if stripped.isdigit() and 1 <= int(stripped) <= len(topic_keys):
                    key = topic_keys[int(stripped) - 1]
                else:
                    token = _choice_token(stripped)
                    matches = [
                        key
                        for key in topic_keys
                        if token
                        in {
                            _choice_token(key),
                            _choice_token(TOPIC_LABELS[key]),
                        }
                    ]
                    if not matches:
                        candidates = {_choice_token(key): key for key in topic_keys} | {
                            _choice_token(label): key for key, label in TOPIC_LABELS.items()
                        }
                        suggestion = difflib.get_close_matches(
                            token,
                            candidates,
                            n=1,
                            cutoff=0.6,
                        )
                        invalid.append(
                            (
                                stripped,
                                TOPIC_LABELS[candidates[suggestion[0]]] if suggestion else None,
                            )
                        )
                        continue
                    key = matches[0]
                if key not in selected:
                    selected.append(key)
            if invalid:
                for _value, suggestion in invalid:
                    detail = f" Did you mean {suggestion}?" if suggestion else ""
                    console.print(
                        f"[yellow]I did not recognize that learning topic.{detail}[/yellow]"
                    )
                console.print(
                    "[yellow]Please try again using the displayed numbers or names.[/yellow]"
                )
                continue
        if selected:
            console.print(
                "[green]✓ Learning topics: "
                f"{', '.join(TOPIC_LABELS[key] for key in selected)}[/green]"
            )
            return selected
        console.print("[yellow]Choose at least one topic, or select Not now.[/yellow]")


def _parse_risk_percent(value: str, maximum: Decimal) -> Decimal:
    cleaned = value.strip().removesuffix("%").strip()
    try:
        percent = Decimal(cleaned)
    except Exception as exc:
        raise ValueError("enter a number such as 0.5 or 1%") from exc
    if not percent.is_finite() or percent <= 0:
        raise ValueError("risk must be greater than 0%")
    if percent > maximum:
        raise ValueError(
            f"risk cannot exceed the configured safety limit of {maximum.normalize()}%"
        )
    return percent


def _prompt_risk_percent(default: Decimal, maximum: Decimal) -> Decimal:
    console.print(
        "\n[bold]Maximum planned risk[/bold]\n"
        f"[dim]The confirmed value becomes the runtime limit for sizing and pre-trade "
        f"checks. The product hard ceiling is {maximum.normalize()}%. "
        "For example, 1% means at most $100 of planned risk per $10,000 of equity.[/dim]"
    )
    while True:
        raw = typer.prompt("Maximum planned risk percent", default=str(default))
        try:
            result = _parse_risk_percent(raw, maximum)
        except ValueError as exc:
            console.print(f"[yellow]{exc}. Please try again.[/yellow]")
            continue
        console.print(f"[green]✓ Maximum planned risk: {result.normalize()}%.[/green]")
        return result


def _clean_onboarding_defaults(
    experience: str,
    settings: Settings,
) -> OnboardingDefaults:
    local_timezone = _local_timezone_name()
    configured_risk = Decimal(str(settings.maximum_trade_risk_percent))
    if (
        not configured_risk.is_finite()
        or configured_risk <= 0
        or configured_risk > ONBOARDING_RISK_CEILING_PERCENT
    ):
        configured_risk = Decimal("1")
    if experience == "beginner":
        return OnboardingDefaults(
            timezone=local_timezone,
            learning_mode="guided",
            markets=("EURUSD",),
            sessions=("New York",),
            trading_style=(
                "Simple trend and pullback setups with predefined entry, stop, and target."
            ),
            goals=(
                "follow the trading plan",
                "risk discipline",
                "journal every trade",
            ),
            maximum_risk_percent=Decimal("0.5"),
        )
    if experience == "intermediate":
        return OnboardingDefaults(
            timezone=local_timezone,
            learning_mode="flexible",
            markets=("XAUUSD",),
            sessions=("New York",),
            trading_style="Discretionary price action with predefined confirmation and risk.",
            goals=("consistency", "measurable edge", "process discipline"),
            maximum_risk_percent=min(configured_risk, Decimal("1")),
        )
    return OnboardingDefaults(
        timezone=local_timezone,
        learning_mode="on_demand",
        markets=("XAUUSD",),
        sessions=("New York",),
        trading_style="Discretionary multi-timeframe price-action trader.",
        goals=("consistency", "measurable edge", "process discipline"),
        maximum_risk_percent=configured_risk,
    )


def _render_beginner_recommendations(defaults: OnboardingDefaults) -> None:
    console.print()
    console.print("[bold cyan]Beginner setup recommendations[/bold cyan]")
    console.print(
        f"  Timezone      {escape_markup(defaults.timezone)} "
        "[dim]— detected from this computer[/dim]"
    )
    console.print(
        f"  Market        {escape_markup(defaults.markets[0])} "
        "[dim]— start with one instrument in practice[/dim]"
    )
    console.print(
        f"  Session       {escape_markup(defaults.sessions[0])} "
        "[dim]— review one repeatable window[/dim]"
    )
    console.print(f"  Style         {escape_markup(defaults.trading_style)}")
    console.print(
        f"  Risk          {defaults.maximum_risk_percent.normalize()}% per trade "
        "[dim]— conservative learning default[/dim]"
    )
    console.print(
        "  Learning      Guided curriculum "
        "[dim]— foundations before advanced frameworks[/dim]"
    )
    console.print(
        "[dim]Press Enter to accept each recommendation, or replace it. "
        "These settings never place a trade.[/dim]"
    )


def _prompt_optional_account_percent(
    label: str,
    default: Decimal | None,
) -> Decimal | None:
    displayed_default = str(default.normalize()) if default is not None else "unknown"
    while True:
        raw = typer.prompt(label, default=displayed_default).strip()
        if raw.casefold() in {"", "unknown", "skip", "none", "not sure"}:
            return None
        try:
            value = Decimal(raw.removesuffix("%").strip())
        except Exception:
            console.print(
                "[yellow]Enter a percentage such as 5 or 5%, or enter unknown.[/yellow]"
            )
            continue
        if not value.is_finite() or value <= 0 or value > 100:
            console.print(
                "[yellow]The percentage must be greater than 0 and at most 100.[/yellow]"
            )
            continue
        return value


def _prompt_optional_account_days(
    label: str,
    default: int | None,
    *,
    allow_zero: bool,
) -> int | None:
    displayed_default = str(default) if default is not None else "unknown"
    while True:
        raw = typer.prompt(label, default=displayed_default).strip()
        if raw.casefold() in {"", "unknown", "skip", "none", "not sure"}:
            return None
        try:
            value = int(raw)
        except ValueError:
            console.print(
                "[yellow]Enter a whole number of days, or enter unknown.[/yellow]"
            )
            continue
        minimum = 0 if allow_zero else 1
        if value < minimum or value > 3_650:
            console.print(
                f"[yellow]Enter a value from {minimum} through 3650.[/yellow]"
            )
            continue
        return value


def _prompt_account_size(default: Decimal) -> Decimal:
    while True:
        raw = typer.prompt(
            "Starting account size (example: 100000)",
            default=format(default.normalize(), "f"),
        ).strip()
        try:
            value = Decimal(raw.replace(",", "").replace("$", "").strip())
        except Exception:
            console.print(
                "[yellow]Enter a positive amount such as 10000 or 100000.[/yellow]"
            )
            continue
        if not value.is_finite() or value <= 0 or value > Decimal("1000000000000"):
            console.print(
                "[yellow]Account size must be positive and no greater than one trillion.[/yellow]"
            )
            continue
        return value


def _prompt_account_currency(default: str) -> str:
    while True:
        value = typer.prompt(
            "Account currency (examples: USD, EUR, USDT)",
            default=default,
        ).strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", value):
            return value
        console.print(
            "[yellow]Use a currency code such as USD, EUR, GBP, or USDT.[/yellow]"
        )


def _prompt_account_constraint(
    existing: AccountConstraintRead | None,
    *,
    trader_timezone: str,
    experience_level: str = "advanced",
) -> AccountConstraintUpsert | None:
    default_type = existing.account_type if existing is not None else "not_configured"
    account_type = _prompt_guided_choice(
        "Trading account",
        ACCOUNT_TYPE_CHOICES,
        default=default_type,
    )
    if account_type == "not_configured":
        return None

    console.print(
        "\n[bold]Account identity and size[/bold]\n"
        "[dim]The starting size is used to translate percentage limits into reminder "
        "amounts. It is not assumed to be current broker equity.[/dim]"
    )
    account_name = _prompt_bounded_text(
        "Account name",
        default=(
            existing.name
            if existing is not None and existing.account_type == account_type
            else (
                "Primary personal or demo account"
                if account_type == "personal"
                else f"Primary {account_type} account"
            )
        ),
        maximum_length=120,
        field_name="Account name",
    )
    account_size = _prompt_account_size(
        existing.account_size
        if existing is not None and existing.account_type == account_type
        else Decimal("100000" if account_type == "prop" else "10000")
    )
    currency = _prompt_account_currency(
        existing.currency
        if existing is not None and existing.account_type == account_type
        else "USD"
    )

    firm_name: str | None = None
    program_name: str | None = None
    phase = "personal"
    if account_type == "prop":
        console.print(
            "\n[bold]Prop firm and phase[/bold]\n"
            "[dim]Examples: FTMO / 100K Challenge / Evaluation. Enter the exact "
            "program names shown by your firm.[/dim]"
        )
        firm_name = _prompt_bounded_text(
            "Prop firm name",
            default=existing.firm_name if existing and existing.firm_name else "Prop firm",
            maximum_length=120,
            field_name="Prop firm name",
        )
        program_name = _prompt_bounded_text(
            "Program or challenge name",
            default=(
                existing.program_name
                if existing and existing.program_name
                else "Trading challenge"
            ),
            maximum_length=120,
            field_name="Program name",
        )
        phase = _prompt_guided_choice(
            "Current prop-program phase",
            PROP_PHASE_CHOICES,
            default=(
                existing.phase
                if existing is not None and existing.phase != "personal"
                else "evaluation"
            ),
        )

    if account_type == "personal" and experience_level == "beginner":
        console.print(
            "[cyan]Basic account details saved for review. Optional loss, drawdown, "
            "news, and holding rules are skipped in beginner setup. You can add a "
            "verified rule later without repeating onboarding.[/cyan]"
        )
        return AccountConstraintUpsert(
            name=account_name,
            account_type=account_type,
            account_size=account_size,
            currency=currency,
            phase=phase,
            rules=AccountRuleLimits(),
        )

    beginner_prop_rules = account_type == "prop" and experience_level == "beginner"
    if beginner_prop_rules and not typer.confirm(
        "Do you have the verified prop-firm loss and drawdown rules available now?",
        default=False,
    ):
        console.print(
            "[cyan]Prop rules were left unverified. The agent will remind you to verify "
            "them before relying on a pre-trade account-compliance check.[/cyan]"
        )
        return AccountConstraintUpsert(
            name=account_name,
            account_type=account_type,
            account_size=account_size,
            currency=currency,
            firm_name=firm_name,
            program_name=program_name,
            phase=phase,
            rules=AccountRuleLimits(),
        )

    previous_rules = existing.rules if existing is not None else AccountRuleLimits()
    console.print(
        "\n[bold]Loss limits and account rules[/bold]\n"
        "[dim]Use the exact percentages published for this account. Enter unknown "
        "when a rule has not been verified; the agent will remind you that it is missing. "
        "These values create reminders and do not prove real-time compliance.[/dim]"
    )
    maximum_daily_loss = _prompt_optional_account_percent(
        "Maximum daily loss percent (example: 5%, or unknown)",
        previous_rules.maximum_daily_loss_percent,
    )
    maximum_total_loss = _prompt_optional_account_percent(
        "Maximum total loss percent (example: 10%, or unknown)",
        previous_rules.maximum_total_loss_percent,
    )
    profit_target = (
        _prompt_optional_account_percent(
            "Profit target percent (example: 8%, or unknown)",
            previous_rules.profit_target_percent,
        )
        if account_type == "prop" and not beginner_prop_rules
        else None
    )
    minimum_days = (
        _prompt_optional_account_days(
            "Minimum trading days (whole number, or unknown)",
            previous_rules.minimum_trading_days,
            allow_zero=True,
        )
        if account_type == "prop" and not beginner_prop_rules
        else None
    )
    maximum_days = (
        _prompt_optional_account_days(
            "Maximum trading days (whole number, or unknown)",
            previous_rules.maximum_trading_days,
            allow_zero=False,
        )
        if account_type == "prop" and not beginner_prop_rules
        else None
    )
    consistency_limit = (
        _prompt_optional_account_percent(
            "Consistency limit percent (or unknown)",
            previous_rules.consistency_limit_percent,
        )
        if account_type == "prop"
        else None
    )
    drawdown_type = _prompt_guided_choice(
        "Drawdown calculation",
        DRAWDOWN_TYPE_CHOICES,
        default=previous_rules.drawdown_type,
    )
    news_trading = (
        "unknown"
        if beginner_prop_rules
        else _prompt_guided_choice(
            "Trading around restricted news",
            ACCOUNT_RULE_POLICY_CHOICES,
            default=previous_rules.news_trading,
        )
    )
    overnight_holding = (
        "unknown"
        if beginner_prop_rules
        else _prompt_guided_choice(
            "Holding positions overnight",
            ACCOUNT_RULE_POLICY_CHOICES,
            default=previous_rules.overnight_holding,
        )
    )
    weekend_holding = (
        "unknown"
        if beginner_prop_rules
        else _prompt_guided_choice(
            "Holding positions over the weekend",
            ACCOUNT_RULE_POLICY_CHOICES,
            default=previous_rules.weekend_holding,
        )
    )
    has_reset_timezone = False if beginner_prop_rules else typer.confirm(
        "Does the account define a daily reset timezone?",
        default=previous_rules.daily_reset_timezone is not None,
    )
    reset_timezone = (
        _prompt_timezone(previous_rules.daily_reset_timezone or trader_timezone)
        if has_reset_timezone
        else None
    )
    custom_default = " | ".join(previous_rules.custom_rules)
    custom_raw = (
        ""
        if beginner_prop_rules
        else typer.prompt(
            "Other account rules (separate with |; examples: "
            "no copy trading | close before rollover)",
            default=custom_default,
        )
    )
    custom_rules = [item.strip() for item in custom_raw.split("|") if item.strip()]
    try:
        return AccountConstraintUpsert(
            name=account_name,
            account_type=account_type,
            account_size=account_size,
            currency=currency,
            firm_name=firm_name,
            program_name=program_name,
            phase=phase,
            rules=AccountRuleLimits(
                maximum_daily_loss_percent=maximum_daily_loss,
                maximum_total_loss_percent=maximum_total_loss,
                profit_target_percent=profit_target,
                minimum_trading_days=minimum_days,
                maximum_trading_days=maximum_days,
                consistency_limit_percent=consistency_limit,
                drawdown_type=drawdown_type,
                news_trading=news_trading,
                overnight_holding=overnight_holding,
                weekend_holding=weekend_holding,
                daily_reset_timezone=reset_timezone,
                custom_rules=custom_rules,
            ),
        )
    except ValidationError:
        console.print(
            "[yellow]One or more account-rule values were not accepted. Check that "
            "custom rules are short plain-text trading restrictions without credentials "
            "or URLs, then configure the account again.[/yellow]"
        )
        return _prompt_account_constraint(
            existing,
            trader_timezone=trader_timezone,
            experience_level=experience_level,
        )


def _render_onboarding_review(
    *,
    display_name: str,
    timezone: str,
    experience: str,
    markets: list[str],
    sessions: list[str],
    trading_style: str,
    goals: list[str],
    maximum_risk: Decimal,
    account: AccountConstraintUpsert | None,
    broker: str,
    news: str,
    tradingview: str,
    learning_mode: str,
    learning_topics: list[str],
) -> None:
    table = Table(title="Review before saving", show_header=False)
    table.add_column("Setting", style="bold")
    table.add_column("Your choice")
    table.add_row("Display name", Text(display_name))
    table.add_row("Timezone", timezone)
    table.add_row("Experience", experience)
    table.add_row("Markets", ", ".join(markets))
    table.add_row("Sessions", Text(", ".join(sessions)))
    table.add_row("Trading style", Text(trading_style))
    table.add_row("Goals", Text(", ".join(goals)))
    table.add_row("Maximum risk", f"{maximum_risk.normalize()}%")
    if account is None:
        table.add_row("Trading account", "Not configured")
    else:
        account_label = (
            "Personal / demo"
            if account.account_type == "personal"
            else f"Prop · {account.phase}"
        )
        table.add_row("Trading account", account_label)
        table.add_row(
            "Account size",
            f"{account.currency} {format(account.account_size.normalize(), 'f')}",
        )
        if account.firm_name:
            table.add_row("Prop firm", Text(account.firm_name))
        if account.program_name:
            table.add_row("Program", Text(account.program_name))
        reminders = account_rule_reminders(account)
        table.add_row(
            "Account rules",
            Text("\n".join(reminders) if reminders else "No limits recorded"),
        )
    table.add_row(
        "Broker",
        next(choice.label for choice in BROKER_CHOICES if choice.key == broker),
    )
    table.add_row(
        "News/calendar",
        next(choice.label for choice in NEWS_CHOICES if choice.key == news),
    )
    table.add_row(
        "TradingView alerts",
        next(
            choice.label
            for choice in TRADINGVIEW_CHOICES
            if choice.key == tradingview
        ),
    )
    table.add_row(
        "Learning",
        next(choice.label for choice in LEARNING_MODE_CHOICES if choice.key == learning_mode),
    )
    table.add_row(
        "Learning topics",
        (
            ", ".join(TOPIC_LABELS[key] for key in learning_topics)
            if learning_topics
            else "Not configured"
        ),
    )
    console.print(table)
    console.print(
        "[dim]No API keys or passwords are stored in this profile. "
        "Nothing has been written yet.[/dim]"
    )


def _run_onboarding(db, settings: Settings) -> bool:
    try:
        scope = _current_scope(db)
    except LookupError:
        scope = _ensure_initial_scope(db, settings)
    saved_profile = get_trader_profile(db, scope=scope)
    console.print("[bold green]Trader onboarding[/bold green]")
    console.print(
        "After the final confirmation, profile answers are stored in PostgreSQL table "
        "`trader_profiles`. Broker/news/TradingView selections are stored in the "
        "private `.env`; "
        "credentials are never stored in the profile. The unfinished wizard is not "
        "sent to a model.\n"
        "Each step explains what it affects. You can enter a number, a displayed name, "
        "or press Enter to accept the default."
    )
    if saved_profile is not None:
        console.print(
            "\n[cyan]Starting with clean recommendations. Previously saved answers "
            "will not be displayed or reused. The saved profile remains unchanged "
            "until you confirm the final review.[/cyan]"
        )
    console.print(
        "\n[bold]Display name[/bold]\n"
        "[dim]How the agent addresses you. Examples: Kyle, KyleRain, NY Gold Trader.[/dim]"
    )
    display_name = _prompt_bounded_text(
        "Display name",
        default="Trader",
        maximum_length=120,
        field_name="Display name",
    )
    experience = _prompt_guided_choice(
        "Experience level",
        EXPERIENCE_CHOICES,
        default="beginner",
    )
    defaults = _clean_onboarding_defaults(experience, settings)
    if experience == "beginner":
        _render_beginner_recommendations(defaults)
    timezone = _prompt_timezone(defaults.timezone)
    learning_mode = _prompt_guided_choice(
        "Teaching and learning",
        LEARNING_MODE_CHOICES,
        default=defaults.learning_mode,
    )
    learning_topics = (
        _prompt_learning_topics(list(all_learning_topics()))
        if learning_mode != "disabled"
        else []
    )
    markets = _prompt_markets(list(defaults.markets))
    sessions = _prompt_sessions(list(defaults.sessions))
    trading_style = _prompt_trading_style(defaults.trading_style)
    goals = _prompt_goals(list(defaults.goals))
    configured_maximum = ONBOARDING_RISK_CEILING_PERCENT
    maximum_risk = _prompt_risk_percent(
        defaults.maximum_risk_percent,
        configured_maximum,
    )
    existing_account = None
    account = _prompt_account_constraint(
        existing_account,
        trader_timezone=timezone,
        experience_level=experience,
    )
    _render_integrations()
    broker = _prompt_guided_choice(
        "Broker data",
        BROKER_CHOICES,
        default=settings.broker_provider,
    )
    news = _prompt_guided_choice(
        "FX news and economic calendar",
        NEWS_CHOICES,
        default=settings.news_provider,
    )
    tradingview = _prompt_guided_choice(
        "TradingView chart alerts",
        TRADINGVIEW_CHOICES,
        default=(
            "enabled"
            if getattr(settings, "tradingview_webhook_enabled", False)
            else "disabled"
        ),
    )
    while True:
        _render_onboarding_review(
            display_name=display_name,
            timezone=timezone,
            experience=experience,
            markets=markets,
            sessions=sessions,
            trading_style=trading_style,
            goals=goals,
            maximum_risk=maximum_risk,
            account=account,
            broker=broker,
            news=news,
            tradingview=tradingview,
            learning_mode=learning_mode,
            learning_topics=learning_topics,
        )
        if typer.confirm(
            "Save this profile and these integration choices?",
            default=True,
        ):
            break
        console.print(
            "[cyan]Nothing has been saved. Choose one field to edit, then you will "
            "return to this review.[/cyan]"
        )
        edit = _prompt_guided_choice(
            "Edit onboarding",
            ONBOARDING_EDIT_CHOICES,
            default="goals",
        )
        if edit == "display_name":
            console.print(
                "\n[bold]Display name[/bold]\n[dim]Examples: Kyle, KyleRain, NY Gold Trader.[/dim]"
            )
            display_name = _prompt_bounded_text(
                "Display name",
                default=display_name,
                maximum_length=120,
                field_name="Display name",
            )
        elif edit == "timezone":
            timezone = _prompt_timezone(timezone)
        elif edit == "experience":
            experience = _prompt_guided_choice(
                "Experience level",
                EXPERIENCE_CHOICES,
                default=experience,
            )
        elif edit == "learning_mode":
            previous_learning_mode = learning_mode
            learning_mode = _prompt_guided_choice(
                "Teaching and learning",
                LEARNING_MODE_CHOICES,
                default=learning_mode,
            )
            if learning_mode == "disabled":
                learning_topics = []
            elif previous_learning_mode == "disabled" or not learning_topics:
                learning_topics = _prompt_learning_topics(list(all_learning_topics()))
        elif edit == "learning_topics":
            if learning_mode == "disabled":
                console.print(
                    "[yellow]Teaching is currently paused. Change Teaching mode "
                    "before selecting curriculum topics.[/yellow]"
                )
            else:
                learning_topics = _prompt_learning_topics(
                    learning_topics or list(all_learning_topics())
                )
        elif edit == "markets":
            markets = _prompt_markets(markets)
        elif edit == "sessions":
            sessions = _prompt_sessions(sessions)
        elif edit == "trading_style":
            trading_style = _prompt_trading_style(trading_style)
        elif edit == "goals":
            goals = _prompt_goals(goals)
        elif edit == "risk":
            maximum_risk = _prompt_risk_percent(
                maximum_risk,
                configured_maximum,
            )
        elif edit == "account":
            account = _prompt_account_constraint(
                account or existing_account,
                trader_timezone=timezone,
                experience_level=experience,
            )
        elif edit == "broker":
            broker = _prompt_guided_choice(
                "Broker data",
                BROKER_CHOICES,
                default=broker,
            )
        elif edit == "news":
            news = _prompt_guided_choice(
                "FX news and economic calendar",
                NEWS_CHOICES,
                default=news,
            )
        elif edit == "tradingview":
            tradingview = _prompt_guided_choice(
                "TradingView chart alerts",
                TRADINGVIEW_CHOICES,
                default=tradingview,
            )
        elif edit == "discard":
            if typer.confirm(
                "Discard all unsaved onboarding changes and exit?",
                default=False,
            ):
                console.print(
                    "[yellow]Nothing was saved. Run `trade onboard` when you are "
                    "ready to try again.[/yellow]"
                )
                return False
            console.print("[green]Returning to the onboarding review.[/green]")

    profile_values = TraderProfileUpsert(
        display_name=display_name,
        timezone=timezone,
        experience_level=experience,
        trading_style=trading_style,
        markets=markets,
        sessions=sessions,
        goals=goals,
        risk_preferences={
            "maximum_trade_risk_percent": float(maximum_risk),
        },
    )

    config_path = default_config_path()
    env_snapshot = snapshot_env_file(config_path)
    try:
        update_env_file(
            config_path,
            {
                "BROKER_PROVIDER": broker,
                "NEWS_PROVIDER": news,
                "MAXIMUM_TRADE_RISK_PERCENT": format(maximum_risk, "f"),
                "TRADINGVIEW_WEBHOOK_ENABLED": str(
                    tradingview == "enabled"
                ).lower(),
            },
        )
        profile = upsert_trader_profile(
            db,
            profile_values,
            scope=scope,
            commit=False,
        )
        if account is None:
            deactivate_account_constraints(
                db,
                profile.id,
                scope=scope,
                commit=False,
            )
        else:
            upsert_active_account_constraint(
                db,
                profile,
                account,
                scope=scope,
                commit=False,
            )
        curriculum = configure_learning_curriculum(
            db,
            profile,
            scope=scope,
            experience_level=experience,
            teaching_mode=None if learning_mode == "disabled" else learning_mode,
            selected_topics=learning_topics,
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        restore_env_file(config_path, env_snapshot)
        raise
    console.print(
        f"[green]Saved profile {escape_markup(profile.display_name)} in PostgreSQL.[/green]"
    )
    console.print(
        f"[green]Runtime maximum planned risk is now "
        f"{maximum_risk.normalize()}%.[/green] "
        "[dim]Restart an already-open `trade` session before relying on the new limit.[/dim]"
    )
    if tradingview == "enabled":
        console.print(
            "[yellow]TradingView alert receiving is enabled, but it is not ready "
            "for public traffic until the HTTPS proxy, mTLS/source-IP verification, "
            "header stripping, and trusted proxy CIDRs are configured.[/yellow]"
        )
    if curriculum is not None:
        state = (
            "paused"
            if learning_mode == "disabled"
            else f"configured in {learning_mode.replace('_', '-')} mode"
        )
        console.print(
            f"[green]Learning curriculum {state}.[/green] "
            "Ask “teach me the next lesson” whenever you want to begin."
        )
    if broker == "oanda":
        console.print(
            "[yellow]OANDA still needs its account ID and API token before live reads. "
            "Ask “help me finish OANDA setup” in chat for the guided steps.[/yellow]"
        )
    if broker == "metatrader":
        console.print(
            "[yellow]MetaTrader still needs the read-only bridge URL, dedicated token, "
            "and account ID before live reads. Ask “help me finish MT5 setup” in chat.[/yellow]"
        )
    if broker in {"ibkr", "alpaca", "twelve-data", "ctrader"}:
        console.print(
            "[yellow]This broker is on the roadmap and not yet runnable in this release. "
            "Use OANDA or MetaTrader for live broker reads today.[/yellow]"
        )
    if news == "trading-economics":
        console.print(
            "[yellow]Trading Economics still needs its API key before calendar refreshes. "
            "Ask “help me finish news setup” in chat.[/yellow]"
        )
    if news == "forex-factory":
        console.print(
            "[green]Forex Factory calendar refresh is ready; no API key is required.[/green]"
        )
    console.print(
        "[green]Setup is complete. Continuing in Trading Agent now.[/green] "
        "[dim]Use natural language or /help; you do not need to leave chat to switch "
        "learning, strategy, model, or development modes.[/dim]"
    )
    return True


def _render_cost_table(settings: Settings, provider_name: str, fallback_model: str) -> None:
    table = Table(title="Configured model costs", show_header=True)
    table.add_column("Mode")
    table.add_column("Model")
    table.add_column("API pricing")
    table.add_column("Note")
    for mode in ("economy", "balanced", "deep"):
        configured = getattr(settings, f"{provider_name}_{mode}_model", None)
        model = configured or fallback_model
        pricing = model_pricing(provider_name, model)
        table.add_row(
            mode,
            model,
            format_pricing(pricing) if pricing else "unknown",
            pricing.note if pricing else "Add pricing before relying on an estimate.",
        )
    console.print(table)
    console.print("[dim]Token estimates are approximate; provider billing is authoritative.[/dim]")


def _assess_ollama_model(
    settings: Settings,
    model: str,
    model_sizes: dict[str, int],
    loaded: frozenset[str],
    *,
    snapshot: ResourceSnapshot | None = None,
) -> ModelFitAssessment | None:
    model_size = model_sizes.get(model, 0)
    if model_size <= 0 or not settings.resource_aware_model_routing:
        return None
    return assess_model_fit(
        model=model,
        model_size_bytes=model_size,
        context_length=settings.ollama_context_length,
        memory_reserve_gb=settings.model_memory_reserve_gb,
        memory_block_percent=settings.model_memory_block_percent,
        swap_block_percent=settings.model_swap_block_percent,
        currently_loaded=model in loaded,
        snapshot=snapshot,
    )


def _render_resource_summary(snapshot: ResourceSnapshot) -> None:
    swap = f"{snapshot.swap_percent:.1f}%" if snapshot.swap_percent is not None else "unknown"
    console.print(
        "[dim]"
        f"{snapshot.platform} resources · "
        f"{snapshot.available_memory_bytes / GIB:.1f} GiB available / "
        f"{snapshot.total_memory_bytes / GIB:.1f} GiB total · "
        f"memory {snapshot.memory_percent:.1f}% · "
        f"swap {swap} · "
        f"disk {snapshot.disk_free_bytes / GIB:.1f} GiB free"
        "[/dim]"
    )


def _render_model_assessment(
    assessment: ModelFitAssessment,
    *,
    compact: bool = False,
) -> None:
    color = {"ok": "green", "warning": "yellow", "block": "red"}[assessment.status]
    if compact:
        message = {
            "ok": f"{assessment.model} is ready.",
            "warning": (
                f"Memory headroom is tight for {assessment.model}; continuing. "
                "Use /model for details."
            ),
            "block": (
                f"{assessment.model} cannot load safely at current memory pressure. "
                "Use /model for details."
            ),
        }[assessment.status]
        console.print(f"[{color}]{message}[/{color}]")
        return
    action = {
        "ok": "ready",
        "warning": "caution",
        "block": "blocked at current pressure",
    }[assessment.status]
    console.print(
        f"[{color}]{assessment.model}: {action} — {assessment.reason}; "
        f"~{assessment.estimated_runtime_bytes / GIB:.1f} GiB estimated runtime, "
        f"{assessment.additional_memory_bytes / GIB:.1f} GiB additional now.[/{color}]"
    )


def _render_ollama_models(
    settings: Settings,
    model_sizes: dict[str, int],
    loaded: frozenset[str],
) -> None:
    installed = frozenset(model_sizes)
    snapshot = resource_snapshot()
    table = Table(title="Local Ollama model profiles")
    table.add_column("Profile")
    table.add_column("Configured model")
    table.add_column("Size")
    table.add_column("Installed")
    table.add_column("Loaded")
    table.add_column("Current fit")
    for profile in ("default", "economy", "balanced", "deep"):
        key = "ollama_model" if profile == "default" else f"ollama_{profile}_model"
        model = getattr(settings, key) or settings.ollama_model
        assessment = _assess_ollama_model(
            settings,
            model,
            model_sizes,
            loaded,
            snapshot=snapshot,
        )
        fit = assessment.status if assessment else "unknown"
        table.add_row(
            profile,
            model,
            (f"{model_sizes[model] / GIB:.1f} GiB" if model_sizes.get(model, 0) else "unknown"),
            "[green]yes[/green]" if model in installed else "[yellow]no[/yellow]",
            "[cyan]yes[/cyan]" if model in loaded else "no",
            {
                "ok": "[green]ready[/green]",
                "warning": "[yellow]caution[/yellow]",
                "block": "[red]blocked now[/red]",
                "unknown": "[dim]unknown[/dim]",
            }[fit],
        )
    console.print(table)
    _render_resource_summary(snapshot)
    console.print(
        "[dim]Fit is recalculated before local inference. It includes model size, "
        "context headroom, configured reserve, current memory/swap pressure, and "
        "whether the model is already loaded.[/dim]"
    )
    extras = sorted(
        installed
        - {
            settings.ollama_model,
            settings.ollama_economy_model or settings.ollama_model,
            settings.ollama_balanced_model or settings.ollama_model,
            settings.ollama_deep_model or settings.ollama_model,
        }
    )
    if extras:
        console.print(f"[dim]Other installed models: {', '.join(extras)}[/dim]")


def _request_status_label(
    prepared: PreparedAgentRequest,
    provider_name: str,
    context_count: int,
) -> str:
    route = prepared.route
    pricing = model_pricing(provider_name, route.model)
    if pricing is None:
        cost_label = "pricing unavailable"
    elif provider_name == "ollama":
        cost_label = "local · $0 API"
    else:
        input_tokens = estimated_request_tokens(
            instructions=prepared.instructions,
            message=prepared.message,
            history=prepared.history,
            tools=TOOLS,
        )
        output_tokens = output_budget_for_mode(route.mode)
        first_round = calculate_cost(
            pricing,
            TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        )
        total_rounds = 1 + _runtime_policy().policy.tool_policy.max_tool_rounds
        tool_round_budget = calculate_cost(
            pricing,
            estimated_multi_round_usage(
                initial_input_tokens=input_tokens,
                output_tokens_per_round=output_tokens,
                rounds=total_rounds,
            ),
        )
        cost_label = (
            f"~{format_usd(first_round)}–{format_usd(tool_round_budget)} estimated"
        )
    context_label = f"{context_count} source" if context_count == 1 else f"{context_count} sources"
    return (
        f"[green]Thinking[/green] · {route.mode} · {route.model} · {context_label} · {cost_label}"
    )


@dataclass(frozen=True)
class ResponseDetails:
    route_label: str
    context_count: int
    provider_name: str
    model: str
    usage: TokenUsage
    references: tuple[UsedReference, ...]
    performance: dict[str, float]


def _usage_cost_label(details: ResponseDetails) -> str:
    pricing = model_pricing(details.provider_name, details.model)
    if details.provider_name == "ollama":
        return "$0 API"
    if pricing and (details.usage.input_tokens or details.usage.output_tokens):
        return f"{format_usd(calculate_cost(pricing, details.usage))} estimated API"
    return "pricing unavailable"


def _render_response_details(details: ResponseDetails) -> None:
    table = Table(title="Response details", show_header=False, box=None)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Route", details.route_label)
    table.add_row(
        "Context",
        (
            f"{details.context_count} harness "
            f"{'resource' if details.context_count == 1 else 'resources'}"
        ),
    )
    table.add_row(
        "Usage",
        (
            f"{details.usage.input_tokens:,} input · "
            f"{details.usage.output_tokens:,} output · "
            f"{details.usage.cached_input_tokens:,} cached"
        ),
    )
    table.add_row("Cost", _usage_cost_label(details))
    if details.performance:
        table.add_row(
            "Performance",
            (
                f"{details.performance.get('total_seconds', 0):g}s total · "
                f"{details.performance.get('load_seconds', 0):g}s load · "
                f"{details.performance.get('output_tokens_per_second', 0):g} tok/s"
            ),
        )
    table.add_row("References", str(len(details.references)))
    console.print(table)
    console.print("[dim]/sources lists provenance · /context lists harness resources[/dim]")


def _release_local_model(
    provider: object,
    model: str | None,
    *,
    announce: bool = False,
) -> bool:
    if not isinstance(provider, OllamaProvider) or not model:
        return False
    provider.unload_model(model)
    if announce:
        console.print(f"[green]Released {model} from memory.[/green]")
    return True


_DOCUMENT_FENCE = re.compile(
    r"```(?P<language>[A-Za-z0-9_-]*)[ \t]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)
_TABLE_DIVIDER_CELL = re.compile(r"^:?-{3,}:?$")


def _looks_like_markdown_document(value: str) -> bool:
    lines = value.splitlines()
    headings = sum(bool(re.match(r"^\s{0,3}#{1,6}\s+", line)) for line in lines)
    tables = any(
        index + 1 < len(lines)
        and "|" in line
        and all(
            _TABLE_DIVIDER_CELL.fullmatch(cell.strip())
            for cell in lines[index + 1].strip().strip("|").split("|")
        )
        for index, line in enumerate(lines)
        if line.strip().startswith("|")
    )
    return headings > 0 and (tables or "**" in value or headings > 1)


def _unwrap_document_fences(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        language = match.group("language").casefold()
        body = match.group("body").strip()
        if language in {"markdown", "md"}:
            return body
        if not language and _looks_like_markdown_document(body):
            return body
        return match.group(0)

    return _DOCUMENT_FENCE.sub(replace, value)


def _markdown_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


_TABLE_HEADER_LABELS = {
    "hypothetical description": "Working idea",
    "encoding": "What must be defined",
    "current state this session window": "Status",
    "action required": "Next step",
    "action required from you before proceeding": "Next step",
}


def _terminal_table_header(value: str) -> str:
    return _TABLE_HEADER_LABELS.get(value.strip().casefold(), value.strip())


def _stack_markdown_tables(value: str) -> str:
    lines = value.splitlines()
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or "|" not in lines[index]:
            rendered.append(lines[index])
            index += 1
            continue
        headers = _markdown_cells(lines[index])
        divider = _markdown_cells(lines[index + 1])
        if (
            len(headers) < 2
            or len(headers) != len(divider)
            or not all(_TABLE_DIVIDER_CELL.fullmatch(cell) for cell in divider)
        ):
            rendered.append(lines[index])
            index += 1
            continue
        index += 2
        rows: list[list[str]] = []
        while index < len(lines) and "|" in lines[index]:
            row = _markdown_cells(lines[index])
            if len(row) != len(headers):
                break
            rows.append(row)
            index += 1
        for row in rows:
            if rendered and rendered[-1]:
                rendered.append("")
            title = row[0].strip()
            if title and len(title) <= 72 and "\n" not in title:
                rendered.append(f"### {title}")
            else:
                rendered.extend((f"**{headers[0]}**", title))
            for header, cell in zip(headers[1:], row[1:], strict=True):
                label = _terminal_table_header(header)
                if label == "What must be defined":
                    cell = re.sub(r"(?i)^need:\s*", "", cell)
                rendered.extend(("", f"**{label}**", cell))
        if not rows:
            rendered.extend(
                [
                    " · ".join(headers),
                    " · ".join(divider),
                ]
            )
    return "\n".join(rendered)


_TERMINAL_CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)
_TERMINAL_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_INTERNAL_IDENTIFIER = re.compile(
    r"(?<![/.])\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b(?:\.\.\.)?"
)
_BRACKETED_INTERNAL_IDENTIFIER = re.compile(
    r"\[(?P<name>[a-z][a-z0-9]*(?:_[a-z0-9]+)+)\]"
)
_INTERNAL_LABELS = {
    "edge_requires_evidence": "evidence requirement",
    "validate_strategy_draft": "strategy review",
    "create_strategy_version": "strategy save",
}


def _humanize_terminal_prose(value: str) -> str:
    """Keep model implementation jargon out of the normal trader-facing display."""

    def friendly_identifier(value: str) -> str:
        name = value.removesuffix("...")
        if name.startswith("create_trade_plan_function_call"):
            return "confirmed journal save"
        return _INTERNAL_LABELS.get(name, name.replace("_", " "))

    def humanize_identifier(match: re.Match[str]) -> str:
        return friendly_identifier(match.group(0))

    def humanize_bracketed(match: re.Match[str]) -> str:
        return friendly_identifier(match.group("name"))

    parts = _TERMINAL_CODE.split(value)
    for index in range(0, len(parts), 2):
        prose = parts[index]
        prose = _BRACKETED_INTERNAL_IDENTIFIER.sub(humanize_bracketed, prose)
        prose = _INTERNAL_IDENTIFIER.sub(humanize_identifier, prose)
        prose = re.sub(
            r"(?i)(?:unavailable\s*)?❌\s*(?:disabled|unavailable)?\s*",
            "Unavailable — ",
            prose,
        )
        prose = re.sub(
            r"(?i)(?:available\s*)?✅\s*(?:available)?\s*",
            "Available — ",
            prose,
        )
        prose = prose.replace("⚠️", "Caution —").replace("⚠", "Caution —")
        parts[index] = prose
    return "".join(parts)


def _space_dense_terminal_questions(value: str) -> str:
    """Make model-generated clarification requests readable in a terminal."""

    parts = _TERMINAL_CODE.split(value)
    for index in range(0, len(parts), 2):
        blocks = re.split(r"(\n[ \t]*\n)", parts[index])
        for block_index in range(0, len(blocks), 2):
            block = blocks[block_index]
            stripped = block.strip()
            if (
                stripped.count("?") < 2
                or any(
                    line.lstrip().startswith(("#", "-", "*", ">", "|"))
                    for line in stripped.splitlines()
                )
            ):
                continue
            sentences = _TERMINAL_SENTENCE_BOUNDARY.split(
                re.sub(r"[ \t]*\n[ \t]*", " ", stripped)
            )
            groups: list[list[str]] = []
            current: list[str] = []
            for sentence in sentences:
                if "?" in sentence and current:
                    groups.append(current)
                    current = []
                current.append(sentence)
            if current:
                groups.append(current)
            if len(groups) < 2:
                continue
            leading = block[: len(block) - len(block.lstrip())]
            trailing = block[len(block.rstrip()) :]
            blocks[block_index] = (
                leading
                + "\n\n".join(" ".join(group) for group in groups)
                + trailing
            )
        parts[index] = "".join(blocks)
    return "".join(parts)


def _terminal_markdown(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    safe = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf", "Zl", "Zp"}
    )
    safe = _unwrap_document_fences(safe)
    safe = _stack_markdown_tables(safe)
    safe = _humanize_terminal_prose(safe)
    safe = _space_dense_terminal_questions(safe)
    safe = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "### ", safe)
    safe = re.sub(r"(?m)^(?:[ \t]*[-*_][ \t]*){3,}$", "", safe)
    safe = re.sub(r"\n{3,}", "\n\n", safe)
    return safe.strip()


def _render_agent_reply(
    reply: str,
    route_label: str,
    context_count: int,
    provider_name: str,
    model: str,
    usage: TokenUsage,
    references: list[UsedReference],
    performance: dict[str, float] | None = None,
) -> ResponseDetails:
    details = ResponseDetails(
        route_label=route_label,
        context_count=context_count,
        provider_name=provider_name,
        model=model,
        usage=usage,
        references=tuple(references),
        performance=dict(performance or {}),
    )
    console.print()
    console.print("[bold green]Trading Agent[/bold green] [bold]❯[/bold]")
    console.print(Markdown(_terminal_markdown(reply)))
    route_parts = [model, route_label.split(" · ", 1)[0]]
    detail_parts: list[str] = []
    if details.performance:
        detail_parts.extend(
            [
                f"{details.performance.get('total_seconds', 0):g}s",
                (f"{details.performance.get('output_tokens_per_second', 0):g} tok/s"),
            ]
        )
    if provider_name != "ollama":
        detail_parts.append(_usage_cost_label(details))
    source_label = (
        f"{len(references)} source" if len(references) == 1 else f"{len(references)} sources"
    )
    detail_parts.extend([source_label, "/details"])
    if console.width < 80:
        console.print(f"[dim]{' · '.join(route_parts)}[/dim]")
        console.print(f"[dim]{' · '.join(detail_parts)}[/dim]")
    else:
        console.print(f"[dim]{' · '.join([*route_parts, *detail_parts])}[/dim]")
    console.print()
    return details


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def _prompt_pretrade_mindset() -> MindsetCheckInCreate:
    while True:
        readiness_text = typer.prompt("Readiness (1-5)", default="3").strip()
        try:
            readiness = int(readiness_text)
        except ValueError:
            console.print("[yellow]Readiness must be a whole number from 1 through 5.[/yellow]")
            continue
        if readiness not in range(1, 6):
            console.print("[yellow]Readiness must be between 1 and 5.[/yellow]")
            continue
        accepted_risk = typer.confirm(
            "Do you fully accept the predefined loss if the stop is reached?",
            default=False,
        )
        emotions = _split_list(
            typer.prompt(
                "Emotion tags (separate with |; examples: calm | fear | FOMO)",
                default="",
            )
        )
        emotional_state = (
            typer.prompt(
                "How are you feeling right now? Your words are saved as written; "
                "profanity is not filtered",
                default="",
            ).strip()
            or None
        )
        note = typer.prompt("Mindset/process note", default="").strip() or None
        try:
            return MindsetCheckInCreate(
                phase="pre_trade",
                readiness=readiness,
                accepted_risk=accepted_risk,
                emotion_tags=emotions,
                emotional_state=emotional_state,
                note=note,
            )
        except ValidationError:
            console.print(
                "[yellow]That check-in was not accepted. Use no more than 20 short "
                "emotion tags, keep free-form fields under 2,000 characters, and do "
                "not paste credentials. Your language and profanity are not filtered. "
                "Please try again.[/yellow]"
            )


def _prompt_plan(setup_name: str | None = None) -> TradePlanCreate:
    market_time = typer.prompt(
        "Market time (ISO-8601 with timezone; blank if unknown)",
        default="",
    )
    sizing_provider = typer.prompt(
        "Sizing provider (blank for manual value-per-price-unit)",
        default="",
    )
    sizing_symbol = typer.prompt("Broker symbol", default="XAU_USD") if sizing_provider else None
    return TradePlanCreate(
        instrument=typer.prompt("Instrument", default="XAUUSD"),
        venue=typer.prompt("Venue", default="OANDA"),
        direction=typer.prompt("Direction (long/short)"),
        setup_name=(
            typer.prompt("Strategy", default=setup_name)
            if setup_name
            else typer.prompt("Setup name")
        ),
        regime=typer.prompt("Regime", default="unknown"),
        session_name=typer.prompt("Session", default="New York"),
        market_time=market_time or None,
        context_timeframe=typer.prompt("Context timeframe", default="4h"),
        trigger_timeframe=typer.prompt("Trigger timeframe", default="5m"),
        entry=Decimal(typer.prompt("Entry")),
        stop=Decimal(typer.prompt("Stop")),
        target=Decimal(typer.prompt("Target")),
        account_equity=Decimal(typer.prompt("Account equity")),
        risk_percent=Decimal(typer.prompt("Risk percent", default="1")),
        value_per_price_unit=(
            Decimal("1") if sizing_provider else Decimal(typer.prompt("Value per price unit"))
        ),
        sizing_provider=sizing_provider or None,
        sizing_symbol=sizing_symbol,
        available_margin=(Decimal(typer.prompt("Available margin")) if sizing_provider else None),
        conversion_rate_to_account=(
            Decimal(typer.prompt("PnL-to-account currency rate", default="1"))
            if sizing_provider
            else Decimal("1")
        ),
        estimated_slippage=(
            Decimal(typer.prompt("Estimated slippage in price units", default="0"))
            if sizing_provider
            else Decimal("0")
        ),
        thesis=typer.prompt("Thesis"),
        invalidation=typer.prompt("Invalidation"),
        observations=_split_list(
            typer.prompt("Visible observations (separate with |)", default="")
        ),
        interpretations=_split_list(
            typer.prompt("Interpretations/hypotheses (separate with |)", default="")
        ),
    )


def _read_plan(path: Path | None) -> TradePlanCreate:
    if path is None:
        return _prompt_plan()
    return TradePlanCreate.model_validate_json(path.read_text())


def _save_plan(request: TradePlanCreate, assume_yes: bool) -> None:
    settings = get_settings()
    maximum_risk = Decimal(str(settings.maximum_trade_risk_percent))
    if request.risk_percent > maximum_risk:
        raise ValueError("requested risk exceeds the configured maximum")
    if request.sizing_provider and request.sizing_symbol:
        upgrade_database()
        with SessionLocal() as db:
            specification = active_instrument_specification(
                db,
                provider=request.sizing_provider,
                external_symbol=request.sizing_symbol,
            )
            sizing = calculate_broker_position_size(
                BrokerPositionSizeRequest(
                    account_equity=request.account_equity,
                    available_margin=request.available_margin,
                    risk_percent=request.risk_percent,
                    entry=request.entry,
                    stop=request.stop,
                    target=request.target,
                    conversion_rate_to_account=request.conversion_rate_to_account,
                    estimated_slippage=request.estimated_slippage,
                    maximum_risk_percent=maximum_risk,
                ),
                specification,
            )
            risk_display = sizing.estimated_loss_at_stop + sizing.estimated_costs
    else:
        sizing = calculate_position_size(request)
        risk_display = sizing.risk_amount
    console.print(
        Panel(
            f"Estimated total risk: ${risk_display}\n"
            f"Quantity: {sizing.quantity}\n"
            f"Planned R: {sizing.planned_r}",
            title="Plan preview",
        )
    )
    _authorize_direct(
        "create_trade_plan",
        request.model_dump(mode="json"),
        mutating=True,
        assume_yes=assume_yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        trade = create_trade_plan(
            db,
            request,
            scope=scope,
            policy_hash=_runtime_policy().content_hash,
            source="cli",
            maximum_risk_percent=maximum_risk,
        )
        _print_model(TradePlanRead.model_validate(trade))


def _prompt_rule_answer(rule_kind: str, text: str) -> bool | None:
    question = "Requirement met" if rule_kind == "requirement" else "Exclusion applies"
    while True:
        answer = (
            typer.prompt(
                f"{question}? {text} (yes/no/unknown)",
            )
            .strip()
            .lower()
        )
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        if answer in {"u", "unknown", "unclear"}:
            return None
        console.print("[yellow]Enter yes, no, or unknown.[/yellow]")


def _prompt_strategy_rule_list(
    heading: str,
    *,
    explanation: str,
    default: str,
    required: bool = True,
) -> list[str]:
    console.print(
        f"\n[bold]{heading}[/bold]\n"
        f"[dim]{explanation} Separate rules with | so commas can remain inside "
        "a rule.[/dim]"
    )
    while True:
        values = _split_list(typer.prompt(heading, default=default))
        if required and not values:
            console.print("[yellow]Enter at least one observable rule.[/yellow]")
            continue
        if len(values) > 20:
            console.print("[yellow]Use no more than 20 rules in this section.[/yellow]")
            continue
        reviewed: list[str] = []
        invalid = False
        for value in values:
            try:
                normalized = validate_profile_text(
                    value,
                    field_name="Strategy rule",
                    maximum_length=500,
                )
            except ValueError as exc:
                console.print(f"[yellow]A strategy rule was not accepted: {exc}.[/yellow]")
                invalid = True
                break
            reviewed.append(_review_trading_style_spelling(normalized))
        if invalid:
            continue
        if len({value.casefold() for value in reviewed}) != len(reviewed):
            console.print("[yellow]Strategy rules cannot contain duplicates.[/yellow]")
            continue
        return reviewed


def _prompt_minimum_planned_r(default: Decimal = Decimal("3")) -> Decimal:
    while True:
        raw = typer.prompt("Minimum planned reward-to-risk", default=str(default))
        try:
            value = Decimal(raw)
        except Exception:
            console.print("[yellow]Enter a number such as 2 or 3.[/yellow]")
            continue
        if not value.is_finite() or value <= 0 or value > 100:
            console.print(
                "[yellow]Minimum reward-to-risk must be greater than 0 and at "
                "most 100.[/yellow]"
            )
            continue
        return value


def _strategy_setup_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if len(key) < 2:
        key = f"setup_{key or 'one'}"
    return key[:64].rstrip("_")


def _prompt_strategy_name(
    default: str = "my-strategy",
    *,
    existing_names: tuple[str, ...] = (),
) -> str:
    while True:
        value = _prompt_bounded_text(
            "Strategy name",
            default=default,
            maximum_length=120,
            field_name="Strategy name",
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,119}", value):
            console.print(
                "[yellow]Use 2-120 letters, numbers, spaces, dots, underscores, or "
                "hyphens, starting with a letter or number.[/yellow]"
            )
            continue
        if value.casefold() in {name.casefold() for name in existing_names}:
            console.print(
                "[yellow]That strategy already exists. Return to the saved-strategy "
                "choice to use it, or enter a distinct name for a separate method.[/yellow]"
            )
            continue
        return value


def _prompt_guided_strategy_definition(
    settings: Settings,
    *,
    existing_names: tuple[str, ...] = (),
) -> tuple[str, str, dict, int]:
    """Build one explicit preflight-ready strategy without model inference."""
    console.print(
        "\n[bold green]Build an exact strategy[/bold green]\n"
        "[dim]This creates an immutable operational definition for consistent "
        "preflight checks. It does not prove an edge; evidence comes later through "
        "backtesting and forward testing. Every answer remains editable only by "
        "creating a new version.[/dim]"
    )
    while True:
        name = _prompt_strategy_name(existing_names=existing_names)
        methodology = _review_trading_style_spelling(
            _prompt_bounded_text(
                "Methodology",
                default="price action",
                maximum_length=160,
                field_name="Methodology",
            )
        )
        objective = _review_trading_style_spelling(
            _prompt_bounded_text(
                "Objective",
                default=(
                    "Trade only the defined setup when context, entry, risk, "
                    "and invalidation rules are satisfied."
                ),
                maximum_length=1000,
                field_name="Strategy objective",
            )
        )
        setup_label = _review_trading_style_spelling(
            _prompt_bounded_text(
                "Setup name",
                default="break and retest",
                maximum_length=160,
                field_name="Setup name",
            )
        )
        setup_key = _strategy_setup_key(setup_label)
        context_requirements = _prompt_strategy_rule_list(
            "Context requirements",
            explanation=(
                "What must already be observable before you look for an entry? "
                "Example: higher-timeframe direction and key levels are marked."
            ),
            default="Higher-timeframe direction and key levels are marked",
        )
        entry_requirements = _prompt_strategy_rule_list(
            "Entry confirmations",
            explanation=(
                "What must visibly happen before entry? Use rules you can answer "
                "yes, no, or unknown in real time."
            ),
            default=(
                "Price reaches the predefined area | "
                "A candle close confirms the trigger | "
                "Entry, stop, target, and invalidation are defined before entry"
            ),
        )
        exclusions = _prompt_strategy_rule_list(
            "Stand-aside conditions",
            explanation=(
                "What disqualifies the trade even when the setup looks attractive?"
            ),
            default=(
                "Required evidence is missing | "
                "Planned reward-to-risk is below the minimum | "
                "High-impact news is inside the configured pre-trade window"
            ),
        )
        maximum_allowed = Decimal(str(settings.maximum_trade_risk_percent))
        strategy_risk = _prompt_risk_percent(
            min(maximum_allowed, Decimal("1")),
            maximum_allowed,
        )
        minimum_r = _prompt_minimum_planned_r()
        caution_tags = _prompt_strategy_rule_list(
            "Mindset caution tags",
            explanation=(
                "Emotion labels that should add caution during this strategy's "
                "preflight. Profanity belongs in the separate emotional-state note."
            ),
            default="fear | hesitation | FOMO | revenge",
            required=False,
        )
        forbidden_concepts = _prompt_strategy_rule_list(
            "Concepts to keep out",
            explanation=(
                "Optional vocabulary from other methods that must not silently "
                "enter this strategy. Leave blank if none."
            ),
            default="",
            required=False,
        )
        while True:
            raw_sample = typer.prompt(
                "Minimum review sample before calling this an edge",
                default="30",
            )
            try:
                minimum_sample = int(raw_sample)
            except ValueError:
                console.print("[yellow]Enter a whole number of at least 5.[/yellow]")
                continue
            if minimum_sample < 5 or minimum_sample > 1000:
                console.print(
                    "[yellow]The review sample must be between 5 and 1,000.[/yellow]"
                )
                continue
            break

        proposed = {
            "methodology": methodology,
            "objective": objective,
            "context": {
                "required": context_requirements,
                "exclusions": [],
            },
            "setups": [
                {
                    "key": setup_key,
                    "requirements": entry_requirements,
                    "exclusions": exclusions,
                }
            ],
            "forbidden_cross_strategy_concepts": forbidden_concepts,
            "mindset": {"caution_emotion_tags": caution_tags},
            "risk": {
                "maximum_risk_percent": strategy_risk,
                "minimum_planned_r": minimum_r,
                "human_confirms_every_trade": True,
            },
        }
        try:
            definition = canonical_strategy_definition(
                proposed,
                maximum_risk_percent=maximum_allowed,
            )
        except (ValidationError, ValueError) as exc:
            console.print(
                f"[yellow]The strategy definition was not valid: {exc}. "
                "Let's review it again.[/yellow]"
            )
            continue

        table = Table(title="Exact strategy to save", show_header=False)
        table.add_column("Field", style="bold")
        table.add_column("Definition")
        table.add_row("Name", Text(name))
        table.add_row("Methodology", Text(methodology))
        table.add_row("Objective", Text(objective))
        table.add_row("Setup", Text(f"{setup_label} · key={setup_key}"))
        table.add_row("Context rules", str(len(context_requirements)))
        table.add_row("Entry confirmations", str(len(entry_requirements)))
        table.add_row("Stand-aside conditions", str(len(exclusions)))
        table.add_row("Maximum risk", f"{strategy_risk.normalize()}%")
        table.add_row("Minimum planned R", str(minimum_r.normalize()))
        table.add_row("Review sample", str(minimum_sample))
        console.print(table)
        for label, values in (
            ("Context", context_requirements),
            ("Entry confirmations", entry_requirements),
            ("Stand aside", exclusions),
            ("Mindset cautions", caution_tags),
            ("Excluded concepts", forbidden_concepts),
        ):
            if values:
                console.print(Text(f"{label}:"))
                for value in values:
                    console.print(Text(f"  • {value}"))
        if typer.confirm(
            "Save and activate this exact immutable strategy?",
            default=True,
        ):
            return name, objective, definition, minimum_sample
        if not typer.confirm("Restart the strategy definition?", default=True):
            raise typer.Abort()


def _prompt_preflight_strategy_selection(summaries) -> str | None:
    console.print(
        "\n[bold]Choose the exact strategy for this trade[/bold]\n"
        "[dim]Only one immutable strategy version can guide a preflight. "
        "You can use a saved strategy, build a new one, or cancel safely.[/dim]"
    )
    for index, summary in enumerate(summaries, start=1):
        console.print(
            f"  [cyan]{index}.[/cyan] [bold]{escape_markup(summary.name)}[/bold] "
            f"v{summary.version} · {summary.knowledge_items} knowledge items"
        )
    create_number = len(summaries) + 1
    cancel_number = create_number + 1
    console.print(
        f"  [cyan]{create_number}.[/cyan] [bold]Build a new strategy[/bold]\n"
        f"  [cyan]{cancel_number}.[/cyan] Cancel and return to chat"
    )
    while True:
        raw = typer.prompt(
            "Choose by number or strategy name",
            default=("1" if summaries else str(create_number)),
        ).strip()
        if raw.isdigit():
            selected = int(raw)
            if 1 <= selected <= len(summaries):
                return summaries[selected - 1].name
            if selected == create_number:
                return "__create__"
            if selected == cancel_number:
                return None
        if raw.casefold() in {"new", "create", "build", "build new"}:
            return "__create__"
        if raw.casefold() in {"cancel", "stop", "back", "exit"}:
            return None
        exact = next(
            (item.name for item in summaries if item.name.casefold() == raw.casefold()),
            None,
        )
        if exact is not None:
            return exact
        suggestions = difflib.get_close_matches(
            raw,
            [item.name for item in summaries],
            n=1,
            cutoff=0.6,
        )
        detail = f" Did you mean {suggestions[0]}?" if suggestions else ""
        console.print(
            f"[yellow]I did not recognize that strategy.{detail} "
            "Choose a displayed number or name.[/yellow]"
        )


def _ensure_preflight_strategy(
    db,
    conversation: ConversationSession,
) -> bool:
    scope = _current_scope(db)
    if active_session_strategy(db, conversation, scope=scope) is not None:
        return True

    console.print(
        "[yellow]This session has no exact strategy, so the agent cannot score "
        "confirmations consistently yet.[/yellow]"
    )
    summaries = list_strategy_summaries(db, scope=scope)
    selected = _prompt_preflight_strategy_selection(summaries)
    if selected is None:
        console.print("[dim]No strategy was selected. No assessment was created.[/dim]")
        return False

    if selected != "__create__":
        playbook, version = resolve_strategy_version(db, selected, scope=scope)
        if not typer.confirm(
            f"Activate {playbook.name} v{version.version} for this session and "
            "continue the preflight?",
            default=True,
        ):
            console.print("[dim]Strategy activation was declined.[/dim]")
            return False
        _authorize_direct(
            "set_session_strategy",
            {
                "session": conversation.name,
                "strategy": playbook.name,
                "version": version.version,
                "content_hash": version.content_hash,
            },
            mutating=True,
            assume_yes=True,
        )
        set_session_strategy(
            db,
            conversation,
            playbook.name,
            scope=scope,
            version=version.version,
        )
        console.print(
            f"[green]Using only {playbook.name} v{version.version} for this "
            "preflight.[/green]"
        )
        return True

    try:
        name, description, definition, minimum_sample = (
            _prompt_guided_strategy_definition(
                get_settings(),
                existing_names=tuple(item.name for item in summaries),
            )
        )
    except typer.Abort:
        console.print("[dim]Strategy creation was cancelled.[/dim]")
        return False
    _authorize_direct(
        "create_strategy_version",
        {
            "name": name,
            "definition": definition,
            "description": description,
            "hypothesis": None,
            "minimum_sample": minimum_sample,
        },
        mutating=True,
        assume_yes=True,
    )
    version = create_validated_strategy_version(
        db,
        scope=scope,
        name=name,
        definition=definition,
        maximum_risk_percent=Decimal(
            str(get_settings().maximum_trade_risk_percent)
        ),
        description=description,
        sample_requirement=minimum_sample,
    )
    _authorize_direct(
        "set_session_strategy",
        {
            "session": conversation.name,
            "strategy": name,
            "version": version.version,
            "content_hash": version.content_hash,
        },
        mutating=True,
        assume_yes=True,
    )
    playbook, version = set_session_strategy(
        db,
        conversation,
        name,
        scope=scope,
        version=version.version,
    )
    console.print(
        f"[green]Created and activated {playbook.name} v{version.version}. "
        "It is an operational definition, not yet a proven edge; collect at least "
        f"{minimum_sample} reviewed samples.[/green]"
    )
    return True


def _render_preflight_assessment(assessment: PreflightAssessment) -> None:
    colors = {
        "eligible": "green",
        "conditional": "yellow",
        "stand_aside": "yellow",
        "blocked": "red",
    }
    label = assessment.rating.replace("_", " ").title()
    console.print(
        Panel(
            f"[{colors[assessment.rating]}]{label}[/{colors[assessment.rating]}]\n"
            f"{assessment.strategy_name} v{assessment.strategy_version} · "
            f"sha256={assessment.strategy_hash[:12]}\n"
            f"Setup: {assessment.setup_key or 'strategy-wide'}\n"
            f"{assessment.disclaimer}",
            title="Pre-trade eligibility",
        )
    )
    score_table = Table(title="Component scores (rule completeness, not win probability)")
    score_table.add_column("Strategy")
    score_table.add_column("Risk")
    score_table.add_column("Mindset")
    score_table.add_column("Evidence")
    score_table.add_column("News")
    score_table.add_row(
        *(
            f"{assessment.component_scores[key]}%"
            for key in ("strategy", "risk", "mindset", "evidence", "news")
        )
    )
    console.print(score_table)
    rule_table = Table(title="Exact active-strategy rules")
    rule_table.add_column("Scope")
    rule_table.add_column("Type")
    rule_table.add_column("Status")
    rule_table.add_column("Rule")
    for item in assessment.rule_results:
        rule_table.add_row(item.scope, item.kind, item.status, item.text)
    if assessment.rule_results:
        console.print(rule_table)
    for title, values, color in (
        ("Hard blockers", assessment.hard_blockers, "red"),
        ("Stand-aside reasons", assessment.stand_aside_reasons, "yellow"),
        ("Missing evidence", assessment.missing_evidence, "yellow"),
    ):
        if values:
            console.print(
                Panel(
                    "\n".join(f"• {value}" for value in values),
                    title=title,
                    border_style=color,
                )
            )
    console.print(f"News state: {assessment.news.status} · {assessment.news.detail}")
    if assessment.alerts:
        console.print(render_pretrade_context(list(assessment.alerts)))


def _render_account_constraint_reminder(
    account: AccountConstraintRead | None,
) -> None:
    console.print()
    console.print(Text("Account rules", style="bold"))
    if account is None:
        console.print(
            "[yellow]  No personal or prop account rules are configured. Run "
            "`trade onboard` before relying on account-limit reminders.[/yellow]"
        )
        return
    label = (
        f"  {account.name} · {account.account_type} · {account.phase} · "
        f"{account.currency} "
        f"{format(account.account_size.normalize(), 'f')} starting size"
    )
    console.print(Text(_literal_terminal_text(label)))
    if account.firm_name:
        firm = account.firm_name
        if account.program_name:
            firm += f" · {account.program_name}"
        console.print(Text(_literal_terminal_text(f"  {firm}"), style="dim"))
    reminders = account_rule_reminders(account)
    for reminder in reminders:
        console.print(Text(_literal_terminal_text(f"  • {reminder}")))
    missing = unverified_account_rules(account)
    if missing:
        console.print(
            Text(
                _literal_terminal_text(
                    "  Verify before trading: " + ", ".join(missing)
                ),
                style="yellow",
            )
        )
    console.print(
        Text(
            "  Reminder only: current daily P&L, equity, and firm-side compliance "
            "have not been verified here.",
            style="dim",
        )
    )


def _render_preflight_recall(recall) -> None:
    console.print()
    console.print(Text("Comparable decision recall", style="bold"))
    if recall.assessment_count == 0:
        console.print(
            "  No prior decisions match this exact strategy version, setup, "
            "and account-rule scope."
        )
        console.print(
            Text(
                "  This is a clean sample; it provides no historical support "
                "or opposition to the current trade.",
                style="dim",
            )
        )
        return

    ratings = ", ".join(
        f"{name.replace('_', ' ')} {count}"
        for name, count in sorted(recall.rating_counts.items())
    )
    decisions = ", ".join(
        f"{name.replace('_', ' ')} {count}"
        for name, count in sorted(recall.decision_counts.items())
    )
    console.print(
        f"  Comparable assessments: {recall.assessment_count} · {ratings}"
    )
    console.print(f"  Human decisions: {decisions}")
    console.print(f"  Evidence status: {recall.evidence_status}")

    if recall.reviewed_outcomes:
        outcome = f"  Reviewed outcomes: {recall.reviewed_outcomes}"
        if recall.average_realized_r is not None:
            outcome += f" · descriptive average {recall.average_realized_r:.2f}R"
        if recall.average_process_score is not None:
            outcome += f" · average process {recall.average_process_score:.1f}"
        console.print(outcome)

    if recall.repeated_cautions:
        console.print()
        console.print(Text("  Repeated cautions", style="yellow"))
        for caution, count in recall.repeated_cautions:
            console.print(
                Text(
                    _literal_terminal_text(f"    • {caution} ({count} times)")
                )
            )

    if recall.recent_decisions:
        console.print()
        console.print(Text("  Recent comparable decisions", style="cyan"))
        for item in recall.recent_decisions:
            outcome = (
                f" · {item.realized_r:.2f}R"
                if item.realized_r is not None
                else ""
            )
            process = (
                f" · process {item.process_score:.1f}"
                if item.process_score is not None
                else ""
            )
            reference = (
                f" · {item.trade_reference}"
                if item.trade_reference is not None
                else ""
            )
            console.print(
                Text(
                    _literal_terminal_text(
                        f"    {item.created_at.date().isoformat()} · "
                        f"{item.rating.replace('_', ' ')} · "
                        f"{item.human_decision.replace('_', ' ')}"
                        f"{reference}{outcome}{process}"
                    )
                )
            )

    console.print()
    console.print(
        Text(
            "  Historical recall is disconfirming context, not a trade signal. "
            "Current evidence must still satisfy every frozen rule.",
            style="dim",
        )
    )


@app.command()
def preflight(
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            exists=True,
            dir_okay=False,
            help="Optional TradePlanCreate JSON; interactive prompts remain for rules and mindset.",
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(help="Named session whose exact active strategy will be used."),
    ] = None,
    setup_key: Annotated[
        str | None,
        typer.Option(help="One setup key from the active immutable strategy."),
    ] = None,
    live_market: Annotated[
        bool,
        typer.Option(
            "--live-market/--no-live-market",
            help="Optionally read an OANDA quote/candles; never places an order.",
        ),
    ] = False,
    candle_timeframe: Annotated[str, typer.Option()] = "M5",
    candle_count: Annotated[int, typer.Option(min=3, max=500)] = 50,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Accept the displayed final journal decision without another prompt.",
        ),
    ] = False,
) -> None:
    """Grade one planned trade against the exact strategy, then journal the decision."""
    settings = get_settings()
    maximum_risk = Decimal(str(settings.maximum_trade_risk_percent))
    try:
        upgrade_database()
        with SessionLocal() as db:
            scope = _current_scope(db)
            conversation = (
                resolve_conversation(db, session, scope=scope)
                if session
                else latest_conversation(db, scope=scope)
            )
            if conversation is None:
                raise LookupError("no session exists; run `trade` and select a strategy first")
            active = active_session_strategy(db, conversation, scope=scope)
            if active is None:
                if not _ensure_preflight_strategy(db, conversation):
                    console.print(
                        "[yellow]Preflight paused without creating an assessment.[/yellow]"
                    )
                    return
                active = active_session_strategy(db, conversation, scope=scope)
                if active is None:
                    raise LookupError(
                        "strategy activation did not persist; no assessment was created"
                    )
            playbook, version = active
            profile = get_trader_profile(db, scope=scope)
            account_constraint = (
                active_account_constraint(db, profile.id, scope=scope)
                if profile is not None
                else None
            )
            _render_account_constraint_reminder(account_constraint)
            definition = version.definition
            setups = definition.get("setups")
            setup_names = (
                [
                    str(item["key"])
                    for item in setups
                    if isinstance(setups, list)
                    and isinstance(item, dict)
                    and isinstance(item.get("key"), str)
                ]
                if isinstance(setups, list)
                else []
            )
            selected_setup = setup_key
            if selected_setup is None and len(setup_names) > 1:
                selected_setup = typer.prompt(f"Setup key ({', '.join(setup_names)})")
            recall = preflight_recall(
                db,
                scope=scope,
                playbook_version_id=version.id,
                setup_key=selected_setup,
                account_constraint_profile_id=(
                    account_constraint.id
                    if account_constraint is not None
                    else None
                ),
                minimum_sample_requirement=version.sample_requirement,
            )
            _render_preflight_recall(recall)
            rules = strategy_rules(definition, setup_key=selected_setup)
            request = (
                _prompt_plan(playbook.name)
                if file is None
                else TradePlanCreate.model_validate_json(file.read_text())
            )
            if request.setup_name.strip().lower() != playbook.name.lower():
                raise ValueError(
                    f"plan strategy must be the active exact strategy: {playbook.name}"
                )
            relevant_currencies = instrument_event_currencies(request.instrument)

            if news_provider_configured(settings):
                try:
                    asyncio.run(refresh_startup_calendar(settings, db))
                except Exception as exc:
                    console.print(
                        f"[yellow]Calendar refresh failed: {type(exc).__name__}. "
                        "Stored freshness is shown below.[/yellow]"
                    )
            now = datetime.now(UTC)
            alerts = pretrade_alerts(
                db,
                "trade",
                currencies=relevant_currencies,
                now=now,
                window_minutes=settings.pretrade_news_window_minutes,
                minimum_importance=settings.pretrade_minimum_event_importance,
            )
            current_news = news_readiness(
                db,
                currencies=relevant_currencies,
                now=now,
                configured=(
                    news_provider_configured(settings)
                ),
            )

            market_context: dict = {}
            if live_market:
                if settings.broker_provider == "none":
                    raise BrokerConfigurationError(
                        "--live-market requires a configured read-only broker"
                    )
                connection = _configured_broker_connection(db, settings)
                connector = create_broker_connector(
                    settings,
                    account=connection.account,
                    connection=connection,
                )
                symbol = request.sizing_symbol or request.instrument

                async def read_market():
                    try:
                        quote = await connector.latest_quote(symbol)
                        candles = await connector.candles(
                            symbol,
                            candle_timeframe,
                            count=candle_count,
                        )
                        return quote, list(candles)
                    finally:
                        await connector.aclose()

                quote, candles = asyncio.run(read_market())
                market_context = {
                    "quote": jsonable_encoder(quote),
                    "features": measure_candle_features(candles),
                }
                console.print(
                    Panel(
                        json.dumps(market_context, indent=2),
                        title="Read-only market evidence",
                    )
                )

            rule_answers = {
                rule.rule_id: _prompt_rule_answer(rule.kind, rule.text) for rule in rules
            }
            mindset_request = _prompt_pretrade_mindset()

            if request.sizing_provider and request.sizing_symbol:
                specification = active_instrument_specification(
                    db,
                    provider=request.sizing_provider,
                    external_symbol=request.sizing_symbol,
                    workspace_id=scope.workspace_id,
                    account_id=scope.account_id,
                )
                sizing = calculate_broker_position_size(
                    BrokerPositionSizeRequest(
                        account_equity=request.account_equity,
                        available_margin=request.available_margin,
                        risk_percent=request.risk_percent,
                        entry=request.entry,
                        stop=request.stop,
                        target=request.target,
                        conversion_rate_to_account=request.conversion_rate_to_account,
                        estimated_slippage=request.estimated_slippage,
                        maximum_risk_percent=maximum_risk,
                    ),
                    specification,
                )
            else:
                sizing = calculate_position_size(request)

            assessment = assess_preflight(
                strategy_name=playbook.name,
                strategy_version=version.version,
                strategy_hash=version.content_hash,
                definition=definition,
                setup_key=selected_setup,
                rule_answers=rule_answers,
                risk_percent=request.risk_percent,
                planned_r=sizing.planned_r,
                configured_maximum_risk_percent=maximum_risk,
                readiness=mindset_request.readiness,
                accepted_risk=mindset_request.accepted_risk,
                emotion_tags=mindset_request.emotion_tags,
                has_thesis=bool(request.thesis.strip()),
                has_invalidation=bool(request.invalidation.strip()),
                observation_count=len(request.observations),
                hypothesis_count=len(request.interpretations),
                news=current_news,
                alerts=alerts,
            )
            _render_preflight_assessment(assessment)

            can_proceed = assessment.rating in {"eligible", "conditional"}
            proceed = can_proceed and (
                yes
                or typer.confirm(
                    "Final human choice: journal this planned trade? "
                    "(This does not place an order.)",
                    default=False,
                )
            )
            decision = (
                "proceed"
                if proceed
                else "stand_aside"
                if assessment.rating in {"stand_aside", "blocked"}
                else "cancelled"
            )
            action = {
                "strategy": playbook.name,
                "strategy_version": version.version,
                "strategy_hash": version.content_hash,
                "rating": assessment.rating,
                "human_decision": decision,
                "risk_percent": str(request.risk_percent),
                "accepted_risk": mindset_request.accepted_risk,
                "order_execution": False,
            }
            _authorize_direct(
                "complete_pretrade_workflow",
                action,
                mutating=True,
                assume_yes=yes,
            )
            persisted = persist_preflight_workflow(
                db,
                scope=scope,
                assessment=assessment,
                playbook_version_id=version.id,
                mindset_request=mindset_request,
                decision=decision,
                policy_hash=_runtime_policy().content_hash,
                market_context=market_context,
                account_constraint_profile_id=(
                    account_constraint.id
                    if account_constraint is not None
                    else None
                ),
                trade_request=request if proceed else None,
                maximum_risk_percent=maximum_risk,
            )
            finalized = persisted.assessment
            mindset = persisted.mindset
            trade = persisted.trade_plan
            console.print(
                Panel(
                    f"Assessment: {finalized.id}\n"
                    f"Decision: {finalized.human_decision}\n"
                    f"Mindset: {mindset.id}\n"
                    f"Trade plan: {trade.reference if trade else 'none'}\n"
                    "Broker order: never submitted",
                    title="Pre-trade audit saved",
                )
            )
    except (
        BrokerConfigurationError,
        LookupError,
        OSError,
        ValidationError,
        ValueError,
    ) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


def _handle_chat_preflight_intent(
    db,
    conversation: ConversationSession,
    message: str,
) -> bool:
    """Offer the guided workflow and consume only explicit near-term trade intent."""
    if not detect_preflight_intent(message):
        return False

    scope = RequestScope(
        workspace_id=conversation.workspace_id,
        account_id=conversation.account_id,
    )
    playbook_version_id = conversation.active_playbook_version_id
    add_turn(
        db,
        conversation,
        "user",
        message,
        scope=scope,
        playbook_version_id=playbook_version_id,
    )
    console.print(
        "[yellow]This sounds like a decision about a possible entry. The guided "
        "preflight checks the exact active strategy, risk, mindset, evidence, and "
        "news. It never places an order.[/yellow]"
    )
    if not typer.confirm("Launch the guided preflight now?", default=True):
        add_turn(
            db,
            conversation,
            "assistant",
            "Guided preflight was offered and declined. No database assessment or "
            "broker order was created.",
            scope=scope,
            playbook_version_id=playbook_version_id,
        )
        console.print("[dim]Preflight skipped. Returning to chat.[/dim]")
        return True

    try:
        if (
            conversation.active_playbook_version_id is None
            and not _ensure_preflight_strategy(db, conversation)
        ):
            add_turn(
                db,
                conversation,
                "assistant",
                "Guided preflight paused because no exact strategy was selected. "
                "No database assessment or broker order was created.",
                scope=scope,
                playbook_version_id=None,
            )
            console.print(
                "[yellow]Preflight paused until an exact strategy is selected. "
                "Returning to chat.[/yellow]"
            )
            return True
        playbook_version_id = conversation.active_playbook_version_id
        preflight(
            file=None,
            session=conversation.name,
            setup_key=None,
            live_market=False,
            candle_timeframe="M5",
            candle_count=50,
            yes=False,
        )
    except typer.Exit as exc:
        add_turn(
            db,
            conversation,
            "assistant",
            (
                "Guided preflight could not be completed "
                f"(status {exc.exit_code}). No order was placed. Returned to chat."
            ),
            scope=scope,
            playbook_version_id=playbook_version_id,
        )
        console.print("[yellow]Preflight ended without completion. Returning to chat.[/yellow]")
        return True
    except Exception as exc:
        add_turn(
            db,
            conversation,
            "assistant",
            (
                f"Guided preflight stopped with {type(exc).__name__}. "
                "No order was placed. Returned to chat."
            ),
            scope=scope,
            playbook_version_id=playbook_version_id,
        )
        console.print(
            f"[red]{type(exc).__name__}: {exc}[/red]\n"
            "[yellow]Preflight stopped safely. Returning to chat.[/yellow]"
        )
        return True

    add_turn(
        db,
        conversation,
        "assistant",
        "Guided preflight completed and recorded the trader's explicit decision. "
        "No broker order was placed. Returned to chat.",
        scope=scope,
        playbook_version_id=playbook_version_id,
    )
    console.print("[dim]Preflight complete. Returning to chat.[/dim]")
    return True


def _confirm_agent_mutation(action: str, arguments: dict) -> bool:
    console.print(Panel(Text(json.dumps(arguments, indent=2)), title=action))
    return typer.confirm("Apply this exact database change?")


def _confirm_agent_external_action(action: str, arguments: dict) -> bool:
    console.print(Panel(Text(json.dumps(arguments, indent=2)), title=action))
    return typer.confirm("Send this exact query to the external search provider?")


def _render_development_session(session: object) -> None:
    validation = getattr(session, "validation", None) or []
    checks = "\n".join(f"{'✓' if item['passed'] else '✗'} {item['command']}" for item in validation)
    detail = (
        f"ID: {session.id}\n"
        f"Status: {session.status}\n"
        f"Branch: {session.branch}\n"
        f"Worktree: {session.worktree}"
    )
    if checks:
        detail += f"\n\nValidation:\n{checks}"
    console.print(Panel(detail, title="Development handoff"))


def _run_development_handoff(
    settings: Settings,
    raw_request: str,
) -> DevelopmentSession | None:
    request = development_request(raw_request)
    commit_behavior = (
        "Validated changes will be committed to the isolated branch."
        if settings.development_approval_flow == "scope_only"
        else "Validated changes will wait for diff review before a local commit."
    )
    console.print(
        Panel(
            f"Change requested:\n{request}\n\n"
            "Scope: Trading Agent source and tests only.\n"
            f"{commit_behavior}\n"
            "Not included: broker order execution, secrets, push, merge, or live restart.",
            title="Confirm development change",
        )
    )
    if not typer.confirm("Is this what you want me to change?"):
        console.print("[yellow]Development handoff cancelled.[/yellow]")
        return None
    console.print("[cyan]Creating an isolated branch and running the coding agent…[/cyan]")
    service = DevelopmentService(settings, _runtime_policy())
    session = service.start(request)
    if settings.development_approval_flow == "scope_only" and session.status == "needs_review":
        session = service.approve(session.id)
    _render_development_session(session)
    if session.summary:
        console.print(Panel(session.summary, title="Coding agent summary"))
    return session


def _run_chat(
    session_reference: str | None,
    new_session: bool,
    session_name: str | None,
) -> None:
    settings = get_settings()
    policy = _runtime_policy()
    for message in ensure_local_services(settings, engine):
        console.print(f"[cyan]{message}[/cyan]")
    if settings.database_auto_migrate:
        try:
            upgrade_database()
        except LegacySchemaDetectedError:
            # Health renders the specific, recovery-oriented legacy adoption error.
            pass
        except Exception as exc:
            # A stopped or unreachable database is explained consistently by health.
            console.print(
                "[yellow]Automatic database upgrade could not run: "
                f"{type(exc).__name__}. Checking database health…[/yellow]"
            )
    startup_scope = None
    try:
        with SessionLocal() as startup_db:
            startup_scope = _ensure_initial_scope(startup_db, settings)
    except Exception:
        # Health below remains the single diagnostic surface for unavailable,
        # legacy, or otherwise unusable databases.
        startup_scope = None
    else:
        settings = get_settings()
    report = check_health(
        settings,
        engine,
        policy=policy,
        model_smoke_test=settings.startup_model_smoke_test,
        scope=startup_scope,
    )
    _render_startup_health(report)
    if not report.ready:
        console.print(
            "[red]Interactive chat requires PostgreSQL. Start it locally or configure Neon, "
            "then run `trading-agent health`.[/red]"
        )
        raise typer.Exit(1)
    try:
        provider = create_model_provider(settings)
    except ProviderConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if session_reference and (new_session or session_name):
        console.print("[red]Use either --session or --new/--name, not both.[/red]")
        raise typer.Exit(2)

    with SessionLocal() as db:
        scope = _current_scope(db)
        if (
            settings.startup_news_sync
            and news_provider_configured(settings)
        ):
            try:
                refreshed = asyncio.run(refresh_startup_calendar(settings, db))
            except Exception as exc:
                console.print(
                    f"[yellow]Startup calendar refresh failed: {type(exc).__name__}[/yellow]"
                )
            else:
                console.print(
                    f"[green]✓ Economic calendar refreshed · {refreshed} new events[/green]"
                )
        if get_trader_profile(db, scope=scope) is None:
            console.print(
                "[yellow]No trader profile exists. Guided onboarding connects your "
                "style, markets, integrations, and strategy imports to PostgreSQL.[/yellow]"
            )
            if typer.confirm("Run onboarding now?", default=True):
                if _run_onboarding(db, settings):
                    get_settings.cache_clear()
                    settings = get_settings()
        current_mode: AgentMode = settings.agent_mode
        current_model_override: str | None = None
        last_runtime_model: str | None = None
        last_response_details: ResponseDetails | None = None
        conversation = (
            resolve_conversation(db, session_reference, scope=scope)
            if session_reference
            else None
        )
        if session_reference and conversation is None:
            console.print(f"[red]Conversation {session_reference} was not found.[/red]")
            raise typer.Exit(1)
        if conversation is None:
            conversation = (
                create_conversation(db, name=session_name, scope=scope)
                if new_session
                else latest_conversation(db, scope=scope)
                or create_conversation(db, scope=scope)
            )

        agent = TradingAgent(
            settings=settings,
            db=db,
            engine=engine,
            confirm_mutation=_confirm_agent_mutation,
            confirm_external_action=_confirm_agent_external_action,
            provider=provider,
            policy=policy,
            scope=scope,
            active_playbook_version_id=conversation.active_playbook_version_id,
        )
        active_strategy = active_session_strategy(db, conversation, scope=scope)
        console.print()
        console.print("[bold green]Trading Agent[/bold green]")
        console.print(
            f"[dim]Session {conversation.name} · {provider.name}/{provider.model} "
            f"· mode {current_mode}[/dim]"
        )
        if active_strategy is not None:
            console.print(
                f"[green]Strategy isolation: {active_strategy[0].name} "
                f"v{active_strategy[1].version} only[/green]"
            )
        else:
            console.print(
                "[yellow]No active strategy · use /strategy use NAME before "
                "strategy-specific guidance[/yellow]"
            )
        startup_memory = build_startup_memory(db, conversation, scope=scope)
        startup_memory_pending = startup_memory.has_content
        _render_startup_memory(startup_memory)
        transcript = conversation_transcript(
            db,
            conversation,
            scope=scope,
            limit=settings.model_history_turn_limit,
        )
        workflow_checkpoint = infer_workflow_checkpoint(
            [item["content"] for item in transcript if item.get("role") == "user"]
        )
        if workflow_checkpoint is not None:
            target = (
                f" · {workflow_checkpoint.instrument}"
                if workflow_checkpoint.instrument
                else ""
            )
            console.print(f"[bold]Continuing:[/bold] {workflow_checkpoint.label}{target}")
            console.print(
                "[dim]Say continue, ask what is missing, or tell me what changed.[/dim]"
            )
        else:
            console.print(
                "[bold]What are you working on?[/bold] Tell me naturally—preparing, "
                "checking a chart, evaluating a trade, or reviewing results."
            )
        console.print(
            "[dim]/help commands · /onboard update setup · /examples starter prompts "
            "· /cost model pricing "
            "· /memory saved recall · /learn curriculum · /model · /model use NAME · "
            "for local overrides"
            " · /details response audit "
            "· /exit leave[/dim]\n"
        )
        while True:
            try:
                message = console.input(
                    "[bold cyan]You[/bold cyan] [bold]❯[/bold] "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not message:
                continue
            if message in {"/exit", "/quit"}:
                break
            if message == "/help":
                console.print(
                    "/exit · leave\n"
                    "/health · run diagnostics\n"
                    "/onboard · update guided setup and return here\n"
                    "/examples · show starter prompts\n"
                    "/cost · show configured model prices\n"
                    "/details · show the full response audit and performance\n"
                    "/sources · show references used for the last response\n"
                    "/context · show harness resources selected for the last response\n"
                    "/memory · show source-backed goals and recent records in scope\n"
                    "/memory use · include bounded recall in the next model request\n"
                    "/memory off · cancel pending recall\n"
                    "/strategy · show active isolated strategy\n"
                    "/strategy use NAME · switch to exactly one strategy version\n"
                    "/strategy clear · disable strategy-specific retrieval\n"
                    "/learn · show curriculum and next lesson\n"
                    "/learn LESSON · begin a sourced teaching conversation\n"
                    "/mode auto|economy|balanced|deep · choose model effort\n"
                    "/model · show local model profiles\n"
                    "/model use NAME · override the local model for this session\n"
                    "/model auto · return to automatic profile routing\n"
                    "/model unload · release this session's local model from memory\n"
                    "/develop <change> · hand a software change to the coding agent\n"
                    "Clear software-change requests also offer a development handoff.\n"
                    "Everything else is natural language; include a local chart path when needed."
                )
                continue
            if message == "/onboard":
                if _run_onboarding(db, settings):
                    get_settings.cache_clear()
                    settings = get_settings()
                    agent.settings = settings
                    current_mode = settings.agent_mode
                    startup_memory = build_startup_memory(
                        db,
                        conversation,
                        scope=scope,
                    )
                    startup_memory_pending = False
                    console.print("[green]Setup updated. You are still in the same chat.[/green]")
                continue
            if message == "/examples":
                _render_starter_prompts()
                continue
            if message == "/cost":
                _render_cost_table(settings, provider.name, provider.model)
                continue
            if message == "/details":
                if last_response_details is None:
                    console.print("No response details recorded yet.")
                else:
                    _render_response_details(last_response_details)
                continue
            if message == "/sources":
                if not agent.last_references:
                    console.print("No response references recorded yet.")
                else:
                    for reference in agent.last_references:
                        timestamp = f" · {reference.retrieved_at}" if reference.retrieved_at else ""
                        console.print(
                            Text(
                                f"{reference.kind}: {reference.label} — "
                                f"{reference.locator}{timestamp}"
                            )
                        )
                continue
            if message == "/health":
                _render_health(check_health(settings, engine))
                continue
            if message == "/context":
                paths = agent.last_harness_context.paths
                console.print("\n".join(paths) if paths else "No task context selected yet.")
                continue
            if message == "/memory":
                startup_memory = build_startup_memory(db, conversation, scope=scope)
                _render_startup_memory(startup_memory, detailed=True)
                continue
            if message == "/memory use":
                startup_memory = build_startup_memory(db, conversation, scope=scope)
                if not startup_memory.has_content:
                    console.print("[dim]There is no saved recall in this scope yet.[/dim]")
                    continue
                console.print(
                    Text(
                        "This will send the bounded recall shown by /memory to "
                        f"{provider.name}/{provider.model} with your next request. "
                        "Raw prior chat, journal notes, and emotional-state prose are excluded."
                    )
                )
                if typer.confirm("Include it in the next model request?", default=False):
                    startup_memory_pending = True
                    console.print(
                        "[green]Recall is ready for the next request only.[/green]"
                    )
                else:
                    startup_memory_pending = False
                    console.print("[dim]Recall was not enabled.[/dim]")
                continue
            if message == "/memory off":
                startup_memory_pending = False
                console.print("[dim]Pending recall was cleared.[/dim]")
                continue
            if message == "/learn":
                try:
                    _, curriculum, learning_scope = _learning_context(db)
                    _render_learning_curriculum(
                        curriculum_read(
                            db,
                            curriculum,
                            scope=learning_scope,
                        )
                    )
                except LookupError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
                continue
            if message.startswith("/learn "):
                lesson = message.removeprefix("/learn ").strip()
                message = (
                    f"Teach me {lesson} using my configured curriculum and tiered "
                    "sources. Do not mix educational frameworks into my active "
                    "execution strategy."
                )
            if message == "/strategy":
                active_strategy = active_session_strategy(
                    db,
                    conversation,
                    scope=scope,
                )
                if active_strategy is None:
                    console.print("No strategy is active for this session.")
                else:
                    console.print(
                        f"{active_strategy[0].name} v{active_strategy[1].version} · "
                        f"sha256={active_strategy[1].content_hash[:12]}"
                    )
                continue
            if message.startswith("/strategy use "):
                strategy_name = message.removeprefix("/strategy use ").strip()
                try:
                    proposed_playbook, proposed_version = resolve_strategy_version(
                        db,
                        strategy_name,
                        scope=scope,
                    )
                    _authorize_direct(
                        "set_session_strategy",
                        {
                            "session": conversation.name,
                            "strategy": proposed_playbook.name,
                            "version": proposed_version.version,
                            "content_hash": proposed_version.content_hash,
                        },
                        mutating=True,
                    )
                    active_strategy = set_session_strategy(
                        db,
                        conversation,
                        proposed_playbook.name,
                        scope=scope,
                        version=proposed_version.version,
                    )
                except (LookupError, PolicyViolation) as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue
                agent.active_playbook_version_id = active_strategy[1].id
                startup_memory = build_startup_memory(db, conversation, scope=scope)
                startup_memory_pending = False
                console.print(
                    f"[green]Strategy isolation switched to "
                    f"{active_strategy[0].name} v{active_strategy[1].version} only.[/green]"
                )
                _render_startup_memory(startup_memory)
                continue
            if message == "/strategy clear":
                previous = active_session_strategy(db, conversation, scope=scope)
                try:
                    _authorize_direct(
                        "clear_session_strategy",
                        {
                            "session": conversation.name,
                            "previous_strategy": (
                                None
                                if previous is None
                                else {
                                    "name": previous[0].name,
                                    "version": previous[1].version,
                                    "content_hash": previous[1].content_hash,
                                }
                            ),
                        },
                        mutating=True,
                    )
                except PolicyViolation as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue
                set_session_strategy(db, conversation, None, scope=scope)
                agent.active_playbook_version_id = None
                active_strategy = None
                startup_memory = build_startup_memory(db, conversation, scope=scope)
                startup_memory_pending = False
                console.print(
                    "[green]Strategy context cleared; strategy knowledge will not be "
                    "retrieved.[/green]"
                )
                _render_startup_memory(startup_memory)
                continue
            if message.startswith("/mode"):
                requested_mode = message.removeprefix("/mode").strip()
                if requested_mode not in {"auto", "economy", "balanced", "deep"}:
                    console.print("[red]Use /mode auto|economy|balanced|deep[/red]")
                    continue
                current_mode = requested_mode  # type: ignore[assignment]
                console.print(f"[green]Model mode is now {current_mode}.[/green]")
                continue
            if message == "/model":
                if not isinstance(provider, OllamaProvider):
                    console.print(
                        f"Current provider is {provider.name}. Use /mode for configured "
                        "API model tiers."
                    )
                    continue
                try:
                    _render_ollama_models(
                        settings,
                        provider.installed_model_sizes(),
                        provider.loaded_models(),
                    )
                except ProviderConfigurationError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue
                if current_model_override:
                    console.print(f"[green]Session override: {current_model_override}[/green]")
                else:
                    console.print("[dim]Session override: automatic routing[/dim]")
                continue
            if message == "/model auto":
                if isinstance(provider, OllamaProvider) and last_runtime_model:
                    try:
                        _release_local_model(provider, last_runtime_model)
                    except ProviderConfigurationError as exc:
                        console.print(f"[yellow]{exc}[/yellow]")
                    last_runtime_model = None
                current_model_override = None
                console.print("[green]Returned to automatic model-profile routing.[/green]")
                continue
            if message == "/model unload":
                if not isinstance(provider, OllamaProvider):
                    console.print("[dim]No local model is managed by this session.[/dim]")
                    continue
                if last_runtime_model is None:
                    console.print("[dim]This session has no loaded local model.[/dim]")
                    continue
                try:
                    _release_local_model(
                        provider,
                        last_runtime_model,
                        announce=True,
                    )
                except ProviderConfigurationError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
                    continue
                last_runtime_model = None
                continue
            if message.startswith("/model use "):
                if not isinstance(provider, OllamaProvider):
                    console.print(
                        "[red]Direct /model switching is available for local Ollama; "
                        "API providers use configured /mode tiers.[/red]"
                    )
                    continue
                selected_model = message.removeprefix("/model use ").strip()
                try:
                    installed = provider.installed_models()
                except ProviderConfigurationError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue
                if selected_model not in installed:
                    console.print(
                        f"[red]{selected_model} is not installed. In another terminal run "
                        f"`trade models pull {selected_model}`.[/red]"
                    )
                    continue
                if last_runtime_model and last_runtime_model != selected_model:
                    try:
                        _release_local_model(provider, last_runtime_model)
                    except ProviderConfigurationError as exc:
                        console.print(f"[yellow]{exc}[/yellow]")
                    last_runtime_model = None
                try:
                    assessment = _assess_ollama_model(
                        settings,
                        selected_model,
                        provider.installed_model_sizes(),
                        provider.loaded_models(),
                    )
                except ProviderConfigurationError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue
                if assessment is not None:
                    _render_model_assessment(assessment)
                    if assessment.status == "block":
                        console.print(
                            "[red]The session override was not changed. Close memory-heavy "
                            "applications or choose a smaller installed model.[/red]"
                        )
                        continue
                current_model_override = selected_model
                console.print(
                    f"[green]This session now uses {selected_model}; /mode still controls "
                    "reasoning effort.[/green]"
                )
                continue
            if detect_development_intent(message):
                request_playbook_version_id = conversation.active_playbook_version_id
                try:
                    development = _run_development_handoff(settings, message)
                    add_turn(
                        db,
                        conversation,
                        "user",
                        message,
                        scope=scope,
                        playbook_version_id=request_playbook_version_id,
                    )
                    if development is None:
                        add_turn(
                            db,
                            conversation,
                            "assistant",
                            "Development handoff was offered and cancelled.",
                            scope=scope,
                            playbook_version_id=request_playbook_version_id,
                        )
                    else:
                        add_turn(
                            db,
                            conversation,
                            "assistant",
                            (
                                f"Development session {development.id} finished with "
                                f"status {development.status} on branch "
                                f"{development.branch}."
                            ),
                            scope=scope,
                            playbook_version_id=request_playbook_version_id,
                        )
                except Exception as exc:
                    console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
                continue

            request_playbook_version_id = conversation.active_playbook_version_id
            request_id = uuid.uuid4()
            user_turn = add_turn(
                db,
                conversation,
                "user",
                message,
                scope=scope,
                playbook_version_id=request_playbook_version_id,
                request_id=request_id,
                status="pending",
            )
            history = conversation_history(
                db,
                conversation,
                scope=scope,
                playbook_version_id=request_playbook_version_id,
                limit=settings.model_history_turn_limit,
            )
            agent.last_tool_audit = None
            try:
                recent_user_context = " ".join(
                    item["content"]
                    for item in history[-6:]
                    if item.get("role") == "user"
                )
                event_currency_context = f"{recent_user_context} {message}"
                alerts = pretrade_alerts(
                    db,
                    "trade" if detect_preflight_intent(message) else "",
                    currencies=instrument_event_currencies(event_currency_context),
                    window_minutes=settings.pretrade_news_window_minutes,
                    minimum_importance=settings.pretrade_minimum_event_importance,
                )
                alert_references = [
                    UsedReference(
                        kind="calendar",
                        label=alert.title,
                        locator=(alert.source_url or f"economic-event:{alert.event_id}"),
                        retrieved_at=alert.retrieved_at.isoformat(),
                    )
                    for alert in alerts
                ]
                if alerts:
                    impact_names = {0: "Info", 1: "Low", 2: "Medium", 3: "High"}
                    console.print("[bold yellow]News reminder[/bold yellow]")
                    for alert in alerts:
                        timing = (
                            f"{abs(alert.minutes_from_now)}m ago"
                            if alert.minutes_from_now < 0
                            else (
                                "now"
                                if alert.minutes_from_now == 0
                                else f"in {alert.minutes_from_now}m"
                            )
                        )
                        console.print(
                            Text(
                                f"  {timing} · {impact_names[alert.importance]} · "
                                f"{alert.currency or alert.country} · {alert.title}"
                            )
                        )
                evidence_parts: list[str] = []
                evidence_references: list[UsedReference] = []
                workflow_checkpoint = infer_workflow_checkpoint(
                    [
                        item["content"]
                        for item in history
                        if item.get("role") == "user"
                    ]
                    + [message]
                )
                if workflow_checkpoint is not None:
                    evidence_parts.append(workflow_checkpoint.prompt_context())
                if startup_memory_pending:
                    evidence_parts.append(startup_memory.prompt_context())
                    evidence_references.extend(
                        _startup_memory_references(startup_memory)
                    )
                alert_context = render_pretrade_context(alerts)
                if alert_context:
                    evidence_parts.append(alert_context)
                evidence_references.extend(alert_references)
                prepared = agent.prepare(
                    message,
                    history,
                    current_mode,
                    evidence_context="\n\n".join(evidence_parts),
                    evidence_references=evidence_references,
                    model_override=current_model_override,
                )
                if (
                    isinstance(provider, OllamaProvider)
                    and last_runtime_model
                    and last_runtime_model != prepared.route.model
                ):
                    _release_local_model(provider, last_runtime_model)
                    last_runtime_model = None
                if isinstance(provider, OllamaProvider):
                    model_sizes = provider.installed_model_sizes()
                    loaded_models = provider.loaded_models()
                    assessment = _assess_ollama_model(
                        settings,
                        prepared.route.model,
                        model_sizes,
                        loaded_models,
                    )
                    if assessment is not None and assessment.status == "block":
                        if current_model_override:
                            _render_model_assessment(assessment)
                            raise RuntimeError(
                                "explicit local-model override is unsafe at current system pressure"
                            )
                        fallback_model = settings.ollama_economy_model or settings.ollama_model
                        fallback = _assess_ollama_model(
                            settings,
                            fallback_model,
                            model_sizes,
                            loaded_models,
                        )
                        if (
                            fallback is None
                            or fallback.status == "block"
                            or fallback_model == prepared.route.model
                        ):
                            _render_model_assessment(assessment)
                            raise RuntimeError(
                                "no configured local model safely fits the current system pressure"
                            )
                        console.print(
                            f"[yellow]Using {fallback_model} instead of "
                            f"{prepared.route.model} because of memory pressure. "
                            "Use /model for details.[/yellow]"
                        )
                        fallback_route = replace(
                            prepared.route,
                            model=fallback_model,
                            reason=(
                                f"resource-aware fallback from {prepared.route.model}: "
                                f"{assessment.reason}"
                            ),
                        )
                        prepared = replace(prepared, route=fallback_route)
                        agent.last_route = fallback_route
                        assessment = fallback
                    if assessment is not None and assessment.status == "warning":
                        _render_model_assessment(assessment, compact=True)
                request_status = _request_status_label(
                    prepared,
                    provider.name,
                    len(agent.last_harness_context.paths),
                )
                with console.status(request_status, spinner="dots"):
                    reply = agent.respond(
                        message,
                        history,
                        mode=current_mode,
                        prepared=prepared,
                        request_id=request_id,
                        conversation_session_id=conversation.id,
                        user_turn_id=user_turn.id,
                    )
                if isinstance(provider, OllamaProvider):
                    last_runtime_model = prepared.route.model
            except Exception as exc:
                partial = bool(
                    agent.last_tool_audit is not None
                    and agent.last_tool_audit.succeeded
                )
                outcome = "partial" if partial else "failed"
                update_turn_outcome(
                    db,
                    user_turn,
                    scope=scope,
                    status=outcome,
                    error_type=type(exc).__name__,
                )
                add_turn(
                    db,
                    conversation,
                    "assistant",
                    (
                        "The request stopped after at least one confirmed database "
                        "change. The completed tool audit was retained."
                        if partial
                        else "The request failed before a complete response was produced."
                    ),
                    scope=scope,
                    playbook_version_id=request_playbook_version_id,
                    request_id=request_id,
                    status=outcome,
                    error_type=type(exc).__name__,
                )
                console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
                continue
            update_turn_outcome(
                db,
                user_turn,
                scope=scope,
                status="complete",
            )
            add_turn(
                db,
                conversation,
                "assistant",
                reply,
                scope=scope,
                playbook_version_id=request_playbook_version_id,
                request_id=request_id,
                status="complete",
            )
            startup_memory_pending = False
            route = agent.last_route
            route_label = (
                f"{route.mode} · {route.provider}/{route.model}" if route else "unknown route"
            )
            context_paths = agent.last_harness_context.paths
            usage = getattr(provider, "last_usage", TokenUsage())
            last_response_details = _render_agent_reply(
                reply,
                route_label,
                len(context_paths),
                route.provider if route else provider.name,
                route.model if route else provider.model,
                usage,
                agent.last_references,
                getattr(provider, "last_performance", None),
            )
        if (
            settings.ollama_unload_on_exit
            and isinstance(provider, OllamaProvider)
            and (provider.local_runtime or settings.ollama_manage_remote_runtime)
            and last_runtime_model
        ):
            try:
                _release_local_model(provider, last_runtime_model)
            except ProviderConfigurationError as exc:
                console.print(f"[yellow]Local model cleanup failed: {exc}[/yellow]")


@app.callback()
def main(
    ctx: typer.Context,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Resume a session by name or UUID."),
    ] = None,
    new: Annotated[
        bool,
        typer.Option("--new", help="Start a new session instead of resuming the latest."),
    ] = False,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Predictable name for a new session."),
    ] = None,
) -> None:
    """Start the interactive agent when no individual command is supplied."""
    ctx.with_resource(_direct_command_audit_lifecycle())
    try:
        _runtime_policy().assert_unchanged()
    except Exception as exc:
        console.print(f"[red]Runtime policy failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    if ctx.invoked_subcommand is None:
        _run_chat(session, new or bool(name), name)


@app.command()
def chat(
    session: Annotated[
        str | None,
        typer.Option("--session", help="Resume a session by name or UUID."),
    ] = None,
    new: Annotated[
        bool,
        typer.Option("--new", help="Start a new session instead of resuming the latest."),
    ] = False,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Predictable name for a new session."),
    ] = None,
) -> None:
    """Open the interactive agent explicitly."""
    _run_chat(session, new or bool(name), name)


@app.command()
def health(
    strict: Annotated[
        bool,
        typer.Option(help="Exit unsuccessfully when any required check fails."),
    ] = False,
    model_smoke_test: Annotated[
        bool,
        typer.Option(
            "--model-smoke-test",
            help="Generate a tiny local-model response instead of only checking installation.",
        ),
    ] = False,
) -> None:
    """Check policy, model-provider configuration, and database connectivity."""
    _authorize_direct("get_system_health", {})
    report = check_health(
        get_settings(),
        engine,
        policy=_runtime_policy(),
        model_smoke_test=model_smoke_test,
    )
    _render_health(report)
    if strict and not report.ready:
        raise typer.Exit(1)


@app.command("integrations")
def integrations_command(
    verify_live: Annotated[
        bool,
        typer.Option(
            "--verify-live",
            help=(
                "Call every configured provider with bounded read-only checks. "
                "Returned data is not stored."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Skip the live-verification API-usage confirmation.",
        ),
    ] = False,
) -> None:
    """Show what is implemented, configured, tested, and verified with real data."""
    _authorize_direct("verify_integrations", {"verify_live": verify_live})
    if not inspect_schema().current:
        upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        reports = integration_verifications(
            get_settings(),
            db,
            scope=scope,
        )
        if verify_live:
            if not yes and not typer.confirm(
                "Run read-only provider checks? This may consume provider API quota.",
                default=False,
            ):
                console.print("Live verification skipped; showing stored qualification.")
            else:
                reports = asyncio.run(
                    verify_live_integrations(
                        get_settings(), reports, db=db, scope=scope
                    )
                )
        _render_integration_verifications(reports)


@models_app.command("list")
def models_list() -> None:
    """Show configured profiles and models currently installed in Ollama."""
    settings = get_settings()
    provider = OllamaProvider(settings)
    try:
        model_sizes = provider.installed_model_sizes()
        loaded = provider.loaded_models()
    except ProviderConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        provider.client.close()
    _render_ollama_models(settings, model_sizes, loaded)


@models_app.command("pull")
def models_pull(
    model: Annotated[str, typer.Argument(help="Ollama model tag to download.")],
    expected_size_gb: Annotated[
        float | None,
        typer.Option(
            "--expected-size-gb",
            min=0.1,
            help=(
                "Conservative expected download size for tags without a parameter "
                "count, such as `latest`."
            ),
        ),
    ] = None,
) -> None:
    """Download a local model without changing the active profiles."""
    try:
        ollama_profile_settings(model, "default")
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"[cyan]Downloading {model}; model files may be tens of gigabytes…[/cyan]")
    pulled, detail = pull_ollama_model(
        model,
        expected_size_gb=expected_size_gb,
    )
    if not pulled:
        console.print(f"[red]{detail}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{model} is installed.[/green]")


@models_app.command("use")
def models_use(
    model: Annotated[str, typer.Argument(help="Installed Ollama model tag.")],
    tier: Annotated[
        str,
        typer.Option(help="default, economy, balanced, deep, quality (balanced+deep), or all."),
    ] = "quality",
) -> None:
    """Persist an installed model for one or more routing profiles."""
    if tier not in {"default", "economy", "balanced", "deep", "quality", "all"}:
        console.print("[red]Tier must be default, economy, balanced, deep, quality, or all.[/red]")
        raise typer.Exit(2)
    settings = get_settings()
    provider = OllamaProvider(settings)
    try:
        model_sizes = provider.installed_model_sizes()
        installed = frozenset(model_sizes)
        loaded = provider.loaded_models()
    except ProviderConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        provider.client.close()
    if model not in installed:
        console.print(
            f"[red]{model} is not installed. Run `trade models pull {model}` first.[/red]"
        )
        raise typer.Exit(1)
    assessment = _assess_ollama_model(settings, model, model_sizes, loaded)
    if assessment is not None:
        _render_model_assessment(assessment)
        if assessment.status == "block":
            console.print(
                "[yellow]The profile can still be saved. Inference will wait or fall back "
                "until current memory/swap pressure is safe.[/yellow]"
            )
    try:
        values = ollama_profile_settings(model, tier)  # type: ignore[arg-type]
        update_env_file(default_config_path(), values)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(
        f"[green]{model} configured for {tier}. Restart `trade` to make it the "
        "persistent profile selection.[/green]"
    )


def _learning_context(db):
    scope = _current_scope(db)
    profile = get_trader_profile(db, scope=scope)
    if profile is None:
        raise LookupError("no trader profile exists; run `trade onboard` first")
    curriculum = curriculum_for_profile(db, profile.id, scope=scope)
    if curriculum is None:
        raise LookupError(
            "no learning curriculum exists; run `trade onboard` and choose a teaching mode"
        )
    return profile, curriculum, scope


def _render_learning_curriculum(data: dict) -> None:
    progress = data["progress"]
    console.print(
        Panel(
            f"Mode: {data['teaching_mode'].replace('_', '-')}\n"
            f"Level: {data['experience_level']}\n"
            f"Status: {data['status']}\n"
            f"Progress: {progress['completed']}/{progress['total']} "
            f"({progress['percent']}%)",
            title="Trading curriculum",
        )
    )
    table = Table(title="Lessons", show_header=True)
    table.add_column("#", justify="right")
    table.add_column("Reference")
    table.add_column("Lesson")
    table.add_column("Status")
    table.add_column("Framework")
    for module in data["modules"]:
        table.add_row(
            str(module["sequence"]),
            module["reference"],
            module["title"],
            module["status"].replace("_", " "),
            module["framework"] or "general",
        )
    console.print(table)
    next_module = data.get("next_module")
    if next_module:
        console.print(
            f"Next: [cyan]{next_module['reference']}[/cyan] — "
            f"{next_module['title']}\n"
            f"Ask: [bold]teach me {next_module['reference']}[/bold]"
        )
    console.print(
        "[dim]Lessons provide education, not trade signals. Framework lessons never "
        "change an execution strategy unless you explicitly create a new immutable "
        "strategy version.[/dim]"
    )


def _show_learning_status() -> None:
    upgrade_database()
    with SessionLocal() as db:
        try:
            _, curriculum, scope = _learning_context(db)
            _render_learning_curriculum(
                curriculum_read(db, curriculum, scope=scope)
            )
        except LookupError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(2) from exc


@learn_app.callback(invoke_without_command=True)
def learn_root(ctx: typer.Context) -> None:
    """Show the curriculum, progress, and next available lesson."""
    if ctx.invoked_subcommand is None:
        _show_learning_status()


@learn_app.command("status")
def learn_status() -> None:
    """Show the curriculum, progress, and next available lesson."""
    _show_learning_status()


def _change_learning_status(
    lesson: str,
    *,
    status: str,
    note: str,
    yes: bool,
) -> None:
    module_key = lesson.removeprefix("lesson-").strip()
    arguments = {
        "lesson": f"lesson-{module_key}",
        "status": status,
        "note": note,
    }
    _authorize_direct(
        "update_learning_progress",
        arguments,
        mutating=True,
        assume_yes=yes,
    )
    with SessionLocal() as db:
        try:
            _, curriculum, scope = _learning_context(db)
            module = update_learning_module(
                db,
                curriculum,
                module_key,
                scope=scope,
                status=status,
                learner_notes=note,
            )
        except (LookupError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
    console.print(f"[green]{module.title}: {module.status.replace('_', ' ')}.[/green]")


@learn_app.command("start")
def learn_start(
    lesson: Annotated[
        str,
        typer.Argument(help="Lesson reference from `trade learn`."),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Apply the displayed progress update."),
    ] = False,
) -> None:
    """Mark a lesson in progress before starting a teaching conversation."""
    _change_learning_status(lesson, status="in_progress", note="", yes=yes)
    console.print(f"Now ask: [bold]teach me {lesson}[/bold]")


@learn_app.command("complete")
def learn_complete(
    lesson: Annotated[
        str,
        typer.Argument(help="Lesson reference from `trade learn`."),
    ],
    note: Annotated[
        str,
        typer.Option(help="Optional learner note or remaining question."),
    ] = "",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Apply the displayed progress update."),
    ] = False,
) -> None:
    """Mark one lesson completed while preserving notes and source references."""
    _change_learning_status(lesson, status="completed", note=note, yes=yes)


@app.command("onboard")
def onboard_command() -> None:
    """Complete guided setup, then continue directly into the interactive agent."""
    settings = get_settings()
    for message in ensure_local_services(settings, engine):
        console.print(f"[cyan]{message}[/cyan]")
    upgrade_database()
    completed = False
    with SessionLocal() as db:
        try:
            completed = _run_onboarding(db, settings)
        except (ValueError, LookupError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
    if completed:
        get_settings.cache_clear()
        _run_chat(None, False, None)


@app.command("setup")
def setup_agent(
    provider: Annotated[
        str | None,
        typer.Option(help="Model provider name: Ollama, OpenAI, or Anthropic."),
    ] = None,
    model: Annotated[
        str,
        typer.Option(help="Local Ollama model to configure."),
    ] = "qwen3.5:9b",
    config: Annotated[
        Path | None,
        typer.Option(help="Environment file to update."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Use defaults without confirmation."),
    ] = False,
    skip_pull: Annotated[
        bool,
        typer.Option(help="Configure Ollama without downloading the model."),
    ] = False,
    database: Annotated[
        str | None,
        typer.Option(help="Database name: Local PostgreSQL, Neon, or Custom."),
    ] = None,
    broker: Annotated[
        str | None,
        typer.Option(help="Broker name: No broker, OANDA, or MetaTrader."),
    ] = None,
    metatrader_platform: Annotated[
        str | None,
        typer.Option(help="MetaTrader terminal generation: MT4 or MT5."),
    ] = None,
    news: Annotated[
        str | None,
        typer.Option(help="News provider name: No news or Trading Economics."),
    ] = None,
    tradingview: Annotated[
        str | None,
        typer.Option(
            help="TradingView webhook receiver: enabled or disabled."
        ),
    ] = None,
) -> None:
    """Run guided environment setup and install the short `trade` launcher."""
    try:
        selected = (
            _resolve_cli_choice(
                provider,
                MODEL_PROVIDER_CHOICES,
                option_name="model provider",
            )
            if provider
            else (
                "ollama"
                if yes
                else _prompt_guided_choice(
                    "Model provider",
                    MODEL_PROVIDER_CHOICES,
                    default="ollama",
                )
            )
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _render_integrations()
    try:
        selected_database = (
            _resolve_cli_choice(
                database,
                DATABASE_CHOICES,
                option_name="database",
            )
            if database
            else (
                "local"
                if yes
                else _prompt_guided_choice(
                    "Database",
                    DATABASE_CHOICES,
                    default="local",
                )
            )
        )
        selected_broker = (
            _resolve_cli_choice(
                broker,
                BROKER_CHOICES,
                option_name="broker",
            )
            if broker
            else (
                "none"
                if yes
                else _prompt_guided_choice(
                    "Broker data",
                    BROKER_CHOICES,
                    default="none",
                )
            )
        )
        selected_news = (
            _resolve_cli_choice(
                news,
                NEWS_CHOICES,
                option_name="news provider",
            )
            if news
            else (
                "none"
                if yes
                else _prompt_guided_choice(
                    "FX news and economic calendar",
                    NEWS_CHOICES,
                    default="none",
                )
            )
        )
        selected_tradingview = (
            _resolve_cli_choice(
                tradingview,
                TRADINGVIEW_CHOICES,
                option_name="TradingView alerts",
            )
            if tradingview
            else (
                "disabled"
                if yes
                else _prompt_guided_choice(
                    "TradingView chart alerts",
                    TRADINGVIEW_CHOICES,
                    default="disabled",
                )
            )
        )
        selected_metatrader_platform = (
            (
                _resolve_cli_choice(
                    metatrader_platform,
                    METATRADER_PLATFORM_CHOICES,
                    option_name="MetaTrader platform",
                )
                if metatrader_platform
                else (
                    "mt5"
                    if yes
                    else _prompt_guided_choice(
                        "MetaTrader terminal",
                        METATRADER_PLATFORM_CHOICES,
                        default="mt5",
                    )
                )
            )
            if selected_broker == "metatrader"
            else None
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    resolved_config = (config or default_config_path()).expanduser().resolve()
    review = Table(title="Review environment setup", show_header=False)
    review.add_column("Setting", style="bold")
    review.add_column("Selection")
    review.add_row(
        "Model provider",
        next(choice.label for choice in MODEL_PROVIDER_CHOICES if choice.key == selected),
    )
    review.add_row("Local model", model if selected == "ollama" else "Not applicable")
    review.add_row(
        "Database",
        next(choice.label for choice in DATABASE_CHOICES if choice.key == selected_database),
    )
    review.add_row(
        "Broker",
        next(choice.label for choice in BROKER_CHOICES if choice.key == selected_broker),
    )
    if selected_metatrader_platform is not None:
        review.add_row(
            "MetaTrader terminal",
            next(
                choice.label
                for choice in METATRADER_PLATFORM_CHOICES
                if choice.key == selected_metatrader_platform
            ),
        )
    review.add_row(
        "News/calendar",
        next(choice.label for choice in NEWS_CHOICES if choice.key == selected_news),
    )
    review.add_row(
        "TradingView alerts",
        next(
            choice.label
            for choice in TRADINGVIEW_CHOICES
            if choice.key == selected_tradingview
        ),
    )
    review.add_row("Private configuration file", str(resolved_config))
    console.print(review)
    console.print(
        "[dim]Setup writes provider selections only. It never asks for or overwrites "
        "API keys and passwords.[/dim]"
    )
    if not yes and not typer.confirm("Apply this environment setup?", default=True):
        console.print("[yellow]Nothing was changed.[/yellow]")
        return

    try:
        values = provider_settings(selected, model)  # type: ignore[arg-type]
        values.update(
            {
                "DATABASE_MODE": selected_database,
                "BROKER_PROVIDER": selected_broker,
                "NEWS_PROVIDER": selected_news,
                "TRADINGVIEW_WEBHOOK_ENABLED": str(
                    selected_tradingview == "enabled"
                ).lower(),
            }
        )
        if selected_metatrader_platform is not None:
            values["METATRADER_PLATFORM"] = selected_metatrader_platform
        update_env_file(resolved_config, values)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"[green]Configured {selected} in {resolved_config}.[/green]")

    launcher_target = launcher_target_for_interpreter(Path(sys.executable))
    try:
        launcher = install_user_launcher(launcher_target)
    except (FileExistsError, FileNotFoundError) as exc:
        console.print(f"[yellow]Global launcher not installed: {exc}[/yellow]")
    else:
        console.print(f"[green]Installed launcher: {launcher}[/green]")
        hint = shell_path_hint(launcher)
        if hint:
            console.print(f"[yellow]{hint}[/yellow]")

    if selected == "ollama":
        if shutil.which("ollama") is None:
            console.print(f"[yellow]{dependency_guidance('ollama')} Then rerun setup.[/yellow]")
            return
        ok, detail = start_local_service("ollama")
        color = "green" if ok else "yellow"
        console.print(f"[{color}]Ollama service: {detail}[/{color}]")
        should_pull = not skip_pull and (
            yes or typer.confirm(f"Download {model} now?", default=True)
        )
        if should_pull:
            console.print(f"[cyan]Downloading {model}; this can take several minutes…[/cyan]")
            pulled, pull_detail = pull_ollama_model(model)
            color = "green" if pulled else "red"
            console.print(f"[{color}]{pull_detail}[/{color}]")
            if not pulled:
                raise typer.Exit(1)

    if selected in {"openai", "anthropic"}:
        key_name = "OPENAI_API_KEY" if selected == "openai" else "ANTHROPIC_API_KEY"
        console.print(
            f"[yellow]Add {key_name} to {resolved_config}; "
            "setup never reads or writes model keys.[/yellow]"
        )
    if selected_database != "local":
        console.print(
            f"[yellow]Add the private {selected_database} SQLAlchemy DATABASE_URL to "
            f"{resolved_config}; setup does not collect database passwords.[/yellow]"
        )
    if selected_broker == "oanda":
        console.print(
            f"[yellow]Add OANDA_API_TOKEN and OANDA_ACCOUNT_ID to {resolved_config}; "
            "start with OANDA_ENVIRONMENT=practice.[/yellow]"
        )
    if selected_broker == "metatrader":
        console.print(
            f"[yellow]Add METATRADER_BRIDGE_URL, METATRADER_BRIDGE_TOKEN, "
            f"METATRADER_ACCOUNT_ID, and METATRADER_PLATFORM to {resolved_config}; "
            "start with METATRADER_MODE=practice.[/yellow]"
        )
    if selected_news == "trading-economics":
        console.print(f"[yellow]Add TRADING_ECONOMICS_API_KEY to {resolved_config}.[/yellow]")
    if selected_tradingview == "enabled":
        console.print(
            "[yellow]Before exposing the receiver, configure the public HTTPS "
            "proxy, mTLS verification, official source-IP allowlist, header "
            "stripping, and trusted proxy CIDRs described in "
            "docs/tradingview-webhooks.md.[/yellow]"
        )
    console.print(
        "[bold green]Environment setup complete. Reopen Terminal, run `trade onboard`, "
        "then run `trade`.[/bold green]"
    )


@app.command("quickstart")
def quickstart_setup(
    provider: Annotated[
        str,
        typer.Option(help="Model provider name: Ollama, OpenAI, or Anthropic."),
    ] = "ollama",
    model: Annotated[
        str,
        typer.Option(help="Local Ollama model tag to configure."),
    ] = "qwen3.5:9b",
    database: Annotated[
        str,
        typer.Option(help="Database mode: local, neon, or custom."),
    ] = "local",
    broker: Annotated[
        str,
        typer.Option(help="Broker name: none, oanda, or metatrader."),
    ] = "none",
    news: Annotated[
        str,
        typer.Option(help="News provider: none, forex-factory, or trading-economics."),
    ] = "none",
    tradingview: Annotated[
        str,
        typer.Option(help="TradingView alerts: enabled or disabled."),
    ] = "disabled",
    metatrader_platform: Annotated[
        str | None,
        typer.Option(help="MetaTrader terminal generation: MT4 or MT5 (metatrader only)."),
    ] = None,
) -> None:
    """Apply a common setup profile without interactive prompts."""
    try:
        selected_provider = _resolve_cli_choice(
            provider,
            MODEL_PROVIDER_CHOICES,
            option_name="model provider",
        )
        selected_database = _resolve_cli_choice(
            database,
            DATABASE_CHOICES,
            option_name="database",
        )
        selected_broker = _resolve_cli_choice(
            broker,
            BROKER_CHOICES,
            option_name="broker",
        )
        selected_news = _resolve_cli_choice(
            news,
            NEWS_CHOICES,
            option_name="news provider",
        )
        selected_tradingview = _resolve_cli_choice(
            tradingview,
            TRADINGVIEW_CHOICES,
            option_name="TradingView alerts",
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    selected_metatrader_platform: str | None = None
    if selected_broker == "metatrader":
        try:
            selected_metatrader_platform = _resolve_cli_choice(
                metatrader_platform,
                METATRADER_PLATFORM_CHOICES,
                option_name="MetaTrader platform",
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

    resolved_config = default_config_path().expanduser().resolve()
    values = provider_settings(selected_provider, model)
    values.update(
        {
            "DATABASE_MODE": selected_database,
            "BROKER_PROVIDER": selected_broker,
            "NEWS_PROVIDER": selected_news,
            "TRADINGVIEW_WEBHOOK_ENABLED": str(selected_tradingview == "enabled").lower(),
        }
    )
    if selected_metatrader_platform is not None:
        values["METATRADER_PLATFORM"] = selected_metatrader_platform
    update_env_file(resolved_config, values)
    get_settings.cache_clear()

    table = Table(title="Quickstart profile")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row(
        "Model provider",
        next(choice.label for choice in MODEL_PROVIDER_CHOICES if choice.key == selected_provider),
    )
    table.add_row("Local model", model if selected_provider == "ollama" else "Not applicable")
    table.add_row(
        "Database",
        next(choice.label for choice in DATABASE_CHOICES if choice.key == selected_database),
    )
    table.add_row(
        "Broker",
        next(choice.label for choice in BROKER_CHOICES if choice.key == selected_broker),
    )
    table.add_row(
        "News/calendar",
        next(choice.label for choice in NEWS_CHOICES if choice.key == selected_news),
    )
    table.add_row(
        "TradingView alerts",
        next(
            choice.label
            for choice in TRADINGVIEW_CHOICES
            if choice.key == selected_tradingview
        ),
    )
    if selected_metatrader_platform is not None:
        table.add_row("MetaTrader terminal", selected_metatrader_platform)
    table.add_row("Config file", str(resolved_config))
    console.print(table)
    console.print(f"[green]Quickstart profile written to {resolved_config}.[/green]")
    if selected_provider == "ollama":
        console.print(
            "[dim]Run `trade models pull qwen3.5:9b` first if the model is not installed, "
            "then `trade`.[/dim]"
        )


@develop_app.command("start")
def develop_start(
    request: Annotated[str, typer.Argument(help="The software change to make.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Accept the reiterated scope without prompting."),
    ] = False,
) -> None:
    """Create an isolated coding session and leave its changes ready for review."""
    if yes:
        settings = get_settings()
        service = DevelopmentService(settings, _runtime_policy())
        session = service.start(request)
        if settings.development_approval_flow == "scope_only" and session.status == "needs_review":
            session = service.approve(session.id)
        _render_development_session(session)
        if session.summary:
            console.print(Panel(session.summary, title="Coding agent summary"))
        return
    _run_development_handoff(get_settings(), request)


@develop_app.command("status")
def develop_status(
    session_id: Annotated[str, typer.Argument(help="Development session ID.")],
) -> None:
    """Show one development session and its validation result."""
    session = DevelopmentService(get_settings(), _runtime_policy()).get(session_id)
    _render_development_session(session)
    if session.summary:
        console.print(Panel(session.summary, title="Coding agent summary"))


@develop_app.command("diff")
def develop_diff(
    session_id: Annotated[str, typer.Argument(help="Development session ID.")],
) -> None:
    """Print the uncommitted code diff produced by a development session."""
    diff = DevelopmentService(get_settings(), _runtime_policy()).diff(session_id)
    console.print(diff or "[yellow]No uncommitted changes.[/yellow]", markup=False)


@develop_app.command("approve")
def develop_approve(
    session_id: Annotated[str, typer.Argument(help="Development session ID.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Commit the validated change on its isolated branch."),
    ] = False,
) -> None:
    """Commit a reviewed change locally; this never pushes or merges it."""
    if not yes:
        console.print(
            "[red]Review `trading-agent develop diff SESSION_ID`, then repeat with --yes.[/red]"
        )
        raise typer.Exit(2)
    session = DevelopmentService(get_settings(), _runtime_policy()).approve(session_id)
    _render_development_session(session)


@app.command()
def risk(
    account_equity: Annotated[str, typer.Option(prompt=True)],
    risk_percent: Annotated[str, typer.Option(prompt=True)],
    entry: Annotated[str, typer.Option(prompt=True)],
    stop: Annotated[str, typer.Option(prompt=True)],
    value_per_price_unit: Annotated[str, typer.Option(prompt=True)],
    target: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Calculate position size and planned R with deterministic arithmetic."""
    try:
        request = PositionSizeRequest(
            account_equity=account_equity,
            risk_percent=risk_percent,
            entry=entry,
            stop=stop,
            target=target,
            value_per_price_unit=value_per_price_unit,
        )
    except ValidationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "calculate_position_size",
        request.model_dump(mode="json"),
        deterministic=True,
    )
    _print_model(calculate_position_size(request))


@instrument_app.command("configure")
def instrument_configure(
    file: Annotated[
        Path,
        typer.Option("--file", exists=True, dir_okay=False),
    ],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Add a time-versioned broker contract specification from JSON."""
    try:
        request = InstrumentSpecificationCreate.model_validate_json(file.read_text())
    except (OSError, ValidationError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "configure_instrument_specification",
        request.model_dump(mode="json"),
        mutating=True,
        deterministic=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        specification = configure_instrument_specification(db, request)
        _print_model(
            {
                "id": specification.id,
                "effective_from": specification.effective_from,
                "source": specification.source,
            }
        )


@instrument_app.command("risk")
def instrument_risk(
    provider: Annotated[str, typer.Option()],
    symbol: Annotated[str, typer.Option()],
    account_equity: Annotated[str, typer.Option()],
    risk_percent: Annotated[str, typer.Option()],
    entry: Annotated[str, typer.Option()],
    stop: Annotated[str, typer.Option()],
    target: Annotated[str | None, typer.Option()] = None,
    available_margin: Annotated[str | None, typer.Option()] = None,
    conversion_rate: Annotated[str, typer.Option()] = "1",
    slippage: Annotated[str, typer.Option()] = "0",
) -> None:
    """Size from a stored broker contract, spread, fees, margin, and step size."""
    settings = get_settings()
    request = BrokerPositionSizeRequest(
        account_equity=account_equity,
        available_margin=available_margin,
        risk_percent=risk_percent,
        entry=entry,
        stop=stop,
        target=target,
        conversion_rate_to_account=conversion_rate,
        estimated_slippage=slippage,
        maximum_risk_percent=Decimal(str(settings.maximum_trade_risk_percent)),
    )
    _authorize_direct(
        "calculate_broker_position_size",
        request.model_dump(mode="json"),
        deterministic=True,
    )
    upgrade_database()
    with SessionLocal() as db:
        specification = active_instrument_specification(
            db,
            provider=provider,
            external_symbol=symbol,
        )
        _print_model(calculate_broker_position_size(request, specification))


@app.command()
def plan(
    file: Annotated[
        Path | None,
        typer.Option("--file", exists=True, dir_okay=False, help="TradePlanCreate JSON file."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Save without the final prompt.")] = False,
) -> None:
    """Create a trade plan interactively or from JSON."""
    try:
        _save_plan(_read_plan(file), yes)
    except (ValidationError, OSError, ValueError, LookupError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@journal_app.command("add")
def journal_add(
    file: Annotated[
        Path | None,
        typer.Option("--file", exists=True, dir_okay=False, help="TradePlanCreate JSON file."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Save without the final prompt.")] = False,
) -> None:
    """Add a journaled trade plan; equivalent to `trading-agent plan`."""
    try:
        _save_plan(_read_plan(file), yes)
    except (ValidationError, OSError, ValueError, LookupError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@journal_app.command("list")
def journal_list(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    show_internal_ids: Annotated[bool, typer.Option()] = False,
) -> None:
    """List recent journaled plans."""
    _authorize_direct("list_trade_plans", {"limit": limit})
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        trades = list_trade_plans(db, limit=limit, scope=scope)
        display_timezone = _profile_timezone(db, scope)
        table = Table(title="Trade plans")
        table.add_column("Reference")
        table.add_column("Instrument")
        table.add_column("Side")
        table.add_column("Setup")
        table.add_column("Status")
        table.add_column("Created")
        if show_internal_ids:
            table.add_column("Internal UUID")
        for trade in trades:
            values = [
                trade.reference,
                trade.instrument,
                trade.direction,
                trade.setup_name,
                trade.status,
                _format_profile_datetime(trade.created_at, display_timezone),
            ]
            if show_internal_ids:
                values.append(str(trade.id))
            table.add_row(*values)
        console.print(table)


@journal_app.command("show")
def journal_show(trade_id: str) -> None:
    """Show one journaled plan."""
    _authorize_direct("get_trade_plan", {"trade_id": str(trade_id)})
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        try:
            trade = get_trade_plan(db, trade_id, scope=scope)
        except TradeNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(TradePlanRead.model_validate(trade))


def _mindset_strategy_version_id(db, session: str | None) -> uuid.UUID:
    scope = _current_scope(db)
    conversation = (
        resolve_conversation(db, session, scope=scope)
        if session
        else latest_conversation(db, scope=scope)
    )
    if conversation is None or conversation.active_playbook_version_id is None:
        raise LookupError(
            "mindset check-ins require an exact active strategy; start `trade` "
            "and use `/strategy use NAME` first"
        )
    if active_session_strategy(db, conversation, scope=scope) is None:
        raise LookupError("the session's active strategy version no longer exists")
    return conversation.active_playbook_version_id


@mindset_app.command("check")
def mindset_check(
    phase: Annotated[
        str,
        typer.Option(help="pre_session, pre_trade, during_trade, or post_trade."),
    ] = "pre_session",
    readiness: Annotated[int, typer.Option(min=1, max=5)] = 3,
    accepted_risk: Annotated[
        bool,
        typer.Option("--accepted-risk/--not-accepted-risk"),
    ] = False,
    emotion: Annotated[
        list[str] | None,
        typer.Option("--emotion", help="Repeat for each concise emotion tag."),
    ] = None,
    emotional_state: Annotated[
        str | None,
        typer.Option(
            "--emotional-state",
            help="Free-form emotional state; exact language is preserved.",
        ),
    ] = None,
    note: Annotated[str | None, typer.Option(help="Optional process observation.")] = None,
    trade: Annotated[
        str | None,
        typer.Option(help="Optional human-readable trade reference or UUID."),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(help="Named session whose exact active strategy scopes the check-in."),
    ] = None,
    yes: Annotated[bool, typer.Option(help="Record without the final prompt.")] = False,
) -> None:
    """Record a non-diagnostic readiness and risk-acceptance check-in."""
    try:
        request = MindsetCheckInCreate(
            phase=phase.replace("-", "_"),
            readiness=readiness,
            accepted_risk=accepted_risk,
            emotion_tags=emotion or [],
            emotional_state=emotional_state,
            note=note,
            trade_reference=trade,
        )
        _authorize_direct(
            "add_mindset_checkin",
            request.model_dump(mode="json"),
            mutating=True,
            assume_yes=yes,
        )
        upgrade_database()
        with SessionLocal() as db:
            scope = _current_scope(db)
            playbook_version_id = _mindset_strategy_version_id(db, session)
            _print_model(
                create_mindset_check_in(
                    db,
                    request,
                    scope=scope,
                    playbook_version_id=playbook_version_id,
                )
            )
    except ValidationError as exc:
        console.print(
            "[red]The mindset check-in was not accepted. Check the phase, readiness, "
            "tag limits, text length, and make sure no credentials were pasted.[/red]"
        )
        raise typer.Exit(2) from exc
    except (ValueError, LookupError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@mindset_app.command("list")
def mindset_list(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    phase: Annotated[str | None, typer.Option()] = None,
    session: Annotated[
        str | None,
        typer.Option(help="Named session whose exact active strategy scopes results."),
    ] = None,
) -> None:
    """List recent mindset check-ins without interpreting them as diagnoses."""
    try:
        if phase is not None:
            MindsetCheckInCreate(
                phase=phase.replace("-", "_"),
                readiness=1,
                accepted_risk=False,
            )
        _authorize_direct("list_mindset_checkins", {"limit": limit, "phase": phase})
        upgrade_database()
        with SessionLocal() as db:
            scope = _current_scope(db)
            playbook_version_id = _mindset_strategy_version_id(db, session)
            check_ins = list_mindset_check_ins(
                db,
                scope=scope,
                playbook_version_id=playbook_version_id,
                limit=limit,
                phase=phase.replace("-", "_") if phase else None,
            )
            display_timezone = _profile_timezone(db, scope)
        table = Table(title="Mindset check-ins")
        table.add_column("Created")
        table.add_column("Phase")
        table.add_column("Ready")
        table.add_column("Risk accepted")
        table.add_column("Emotions")
        table.add_column("Emotional state")
        table.add_column("Trade")
        table.add_column("Process note")
        for item in check_ins:
            table.add_row(
                _format_profile_datetime(item.created_at, display_timezone),
                item.phase,
                f"{item.readiness}/5",
                "yes" if item.accepted_risk else "no",
                Text(", ".join(item.emotion_tags) or "-"),
                Text(item.emotional_state or "-"),
                Text(item.trade_reference or "-"),
                Text(item.note or "-"),
            )
        console.print(table)
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@account_app.command("list")
def account_list() -> None:
    """List accounts in the configured workspace and show the active default."""
    upgrade_database()
    with SessionLocal() as db:
        workspace = _configured_workspace(db)
        accounts = list_accounts(db, workspace.id, active_only=False)
        try:
            current = _current_scope(db)
        except LookupError:
            current = None
        table = Table(title=f"Trading accounts · {workspace.name}")
        table.add_column("Current")
        table.add_column("Name")
        table.add_column("Broker")
        table.add_column("Mode")
        table.add_column("Currency")
        table.add_column("Status")
        table.add_column("Broker ID")
        for account in accounts:
            table.add_row(
                "yes" if current is not None and account.id == current.account_id else "",
                account.label,
                account.broker,
                account.mode,
                account.currency,
                "active" if account.active else "inactive",
                account.external_account_id,
            )
        console.print(table)
        if current is None:
            console.print(
                "[yellow]No active account is selected. Run `trade onboard` for a "
                "new workspace, or recover/select an archived account.[/yellow]"
            )
            return
        selected = next(account for account in accounts if account.id == current.account_id)
        console.print(
            Text.assemble(
                ("Current internal UUID: ", "bold"),
                str(selected.id),
                "\n",
                ("TradingView webhook path: ", "bold"),
                f"/api/webhooks/tradingview/{selected.id}",
            )
        )


@account_app.command("use")
def account_use(
    account_reference: Annotated[
        str,
        typer.Argument(help="Account label, broker account ID, or internal UUID."),
    ],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Make one account the explicit default for new sessions and queries."""
    upgrade_database()
    with SessionLocal() as db:
        workspace = _configured_workspace(db)
        account = resolve_account(
            db,
            workspace.id,
            account_reference,
            active_only=True,
        )
        if account is None:
            console.print("[red]Account was not found in the configured workspace.[/red]")
            raise typer.Exit(1)
        _authorize_direct(
            "select_trading_account",
            {
                "workspace": workspace.slug,
                "account": account.label,
                "broker": account.broker,
                "mode": account.mode,
            },
            mutating=True,
            assume_yes=yes,
        )
        config_path = default_config_path()
        env_snapshot = snapshot_env_file(config_path)
        try:
            update_env_file(
                config_path,
                {
                    "TRADING_WORKSPACE": workspace.slug,
                    "TRADING_ACCOUNT": str(account.id),
                },
            )
            for candidate in list_accounts(db, workspace.id, active_only=False):
                candidate.is_default = candidate.id == account.id
            db.commit()
        except Exception:
            db.rollback()
            restore_env_file(config_path, env_snapshot)
            raise
        get_settings.cache_clear()
        console.print(
            f"[green]{account.label} is now the account for new sessions, journal "
            "queries, preflight decisions, and memory recall.[/green]"
        )
        console.print(
            "[dim]Existing sessions remain attached to their original account. "
            "Restart `trade` to open or resume sessions under this account.[/dim]"
        )


@account_app.command("recover")
def account_recover(
    account_reference: Annotated[
        str,
        typer.Argument(
            help="Archived account label, broker account ID, or internal UUID."
        ),
    ],
    label: Annotated[
        str | None,
        typer.Option(help="Optional clearer label for the recovered archive."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Reactivate an archived account without moving or relabeling its history."""
    upgrade_database()
    with SessionLocal() as db:
        workspace = _configured_workspace(db)
        account = resolve_account(
            db,
            workspace.id,
            account_reference,
            active_only=False,
        )
        if account is None:
            console.print("[red]Archived account was not found in this workspace.[/red]")
            raise typer.Exit(1)
        if account.active:
            console.print(
                "[yellow]That account is already active. Use `trade account use` "
                "to select it.[/yellow]"
            )
            raise typer.Exit(1)
        new_label = "Recovered legacy archive" if label is None else label.strip()
        if not new_label or len(new_label) > 120:
            console.print("[red]Recovered account label must be 1–120 characters.[/red]")
            raise typer.Exit(2)
        _authorize_direct(
            "select_trading_account",
            {
                "workspace": workspace.slug,
                "account": account.label,
                "recovered_label": new_label,
                "preserve_history": True,
            },
            mutating=True,
            assume_yes=yes,
        )
        config_path = default_config_path()
        env_snapshot = snapshot_env_file(config_path)
        try:
            update_env_file(
                config_path,
                {
                    "TRADING_WORKSPACE": workspace.slug,
                    "TRADING_ACCOUNT": str(account.id),
                },
            )
            for candidate in list_accounts(db, workspace.id, active_only=False):
                candidate.is_default = candidate.id == account.id
            account.label = new_label
            account.active = True
            db.commit()
        except Exception:
            db.rollback()
            restore_env_file(config_path, env_snapshot)
            raise
        get_settings.cache_clear()
        console.print(
            f"[green]{escape_markup(account.label)} is active and selected. "
            "Its existing sessions, alerts, and journal records stayed attached "
            "to the same account identity.[/green]"
        )


@account_app.command("tradingview-secret")
def account_tradingview_secret(
    account_reference: Annotated[
        str | None,
        typer.Option(
            "--account",
            help="Account label, broker account ID, or internal UUID; current by default.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Create or rotate the one-time secret used in this account's alert JSON."""
    upgrade_database()
    with SessionLocal() as db:
        workspace = _configured_workspace(db)
        current = _current_scope(db)
        account = resolve_account(
            db,
            workspace.id,
            account_reference or current.account_id,
            active_only=True,
        )
        if account is None:
            console.print("[red]Account was not found in the configured workspace.[/red]")
            raise typer.Exit(1)
        _authorize_direct(
            "configure_tradingview_webhook",
            {
                "workspace": workspace.slug,
                "account": account.label,
                "operation": "rotate account webhook secret",
            },
            mutating=True,
            assume_yes=yes,
        )
        secret = set_tradingview_webhook_secret(db, account=account)
        console.print(
            Panel(
                Text.assemble(
                    ("TradingView webhook configured for ", "bold"),
                    account.label,
                    "\n\n",
                    ("Webhook path\n", "bold"),
                    f"/api/webhooks/tradingview/{account.id}",
                    "\n\n",
                    ("Add this field to the alert JSON\n", "bold"),
                    f'"webhook_secret": "{secret}"',
                    "\n\n",
                    (
                        "This secret is shown once. PostgreSQL stores only its SHA-256 "
                        "digest. Rotating it immediately invalidates the previous value.",
                        "dim",
                    ),
                ),
                title="TradingView account authentication",
            )
        )
@account_app.command("telegram-secret")
def account_telegram_secret(
    account_reference: Annotated[
        str | None,
        typer.Option(
            "--account",
            help="Account label, broker account ID, or internal UUID; current by default.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Create or rotate the one-time secret used in Telegram webhook payloads."""
    upgrade_database()
    with SessionLocal() as db:
        workspace = _configured_workspace(db)
        current = _current_scope(db)
        account = resolve_account(
            db,
            workspace.id,
            account_reference or current.account_id,
            active_only=True,
        )
        if account is None:
            console.print("[red]Account was not found in the configured workspace.[/red]")
            raise typer.Exit(1)
        _authorize_direct(
            "configure_telegram_secret",
            {
                "workspace": workspace.slug,
                "account": account.label,
                "operation": "rotate account telegram webhook secret",
            },
            mutating=True,
            assume_yes=yes,
        )
        secret = set_chat_webhook_secret(db=db, account=account, platform="telegram")
        console.print(
            Panel(
                Text.assemble(
                    ("Telegram webhook configured for ", "bold"),
                    account.label,
                    "\n\n",
                    ("Webhook path\n", "bold"),
                    f"/api/webhooks/telegram/{account.id}",
                    "\n\n",
                    ("Add this field to the message payload\n", "bold"),
                    f'"webhook_secret": "{secret}"',
                    "\n\n",
                    (
                        "This secret is shown once. PostgreSQL stores only its SHA-256 "
                        "digest. Rotating it immediately invalidates the previous value.",
                        "dim",
                    ),
                ),
                title="Telegram account authentication",
            )
        )


@account_app.command("discord-secret")
def account_discord_secret(
    account_reference: Annotated[
        str | None,
        typer.Option(
            "--account",
            help="Account label, broker account ID, or internal UUID; current by default.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Create or rotate the one-time secret used in Discord webhook payloads."""
    upgrade_database()
    with SessionLocal() as db:
        workspace = _configured_workspace(db)
        current = _current_scope(db)
        account = resolve_account(
            db,
            workspace.id,
            account_reference or current.account_id,
            active_only=True,
        )
        if account is None:
            console.print("[red]Account was not found in the configured workspace.[/red]")
            raise typer.Exit(1)
        _authorize_direct(
            "configure_discord_secret",
            {
                "workspace": workspace.slug,
                "account": account.label,
                "operation": "rotate account discord webhook secret",
            },
            mutating=True,
            assume_yes=yes,
        )
        secret = set_chat_webhook_secret(db=db, account=account, platform="discord")
        console.print(
            Panel(
                Text.assemble(
                    ("Discord webhook configured for ", "bold"),
                    account.label,
                    "\n\n",
                    ("Webhook path\n", "bold"),
                    f"/api/webhooks/discord/{account.id}",
                    "\n\n",
                    ("Add this field to the message payload\n", "bold"),
                    f'"webhook_secret": "{secret}"',
                    "\n\n",
                    (
                        "This secret is shown once. PostgreSQL stores only its SHA-256 "
                        "digest. Rotating it immediately invalidates the previous value.",
                        "dim",
                    ),
                ),
                title="Discord account authentication",
            )
        )


@sessions_app.command("list")
def sessions_list(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    show_internal_ids: Annotated[bool, typer.Option()] = False,
) -> None:
    """List recent interactive sessions."""
    upgrade_database()
    with SessionLocal() as db:
        conversations = list_conversations(db, limit, scope=_current_scope(db))
        table = Table(title="Trading Agent sessions")
        table.add_column("Name")
        table.add_column("Title")
        table.add_column("Updated")
        if show_internal_ids:
            table.add_column("Internal UUID")
        for conversation in conversations:
            values = [
                conversation.name,
                conversation.title,
                str(conversation.updated_at),
            ]
            if show_internal_ids:
                values.append(str(conversation.id))
            table.add_row(*values)
        console.print(table)


@database_app.command("upgrade")
def database_upgrade() -> None:
    """Apply all forward-only PostgreSQL migrations."""
    upgrade_database()
    current, head = schema_revisions()
    console.print(f"[green]Database schema is current: {current or head}[/green]")


@database_app.command("status")
def database_status() -> None:
    """Show the applied and expected schema revisions."""
    state = inspect_schema()
    status = (
        "legacy adoption required"
        if state.legacy_unmanaged
        else "current"
        if state.current
        else "upgrade required"
    )
    _print_model(
        {
            "status": status,
            "current": state.current_revision,
            "head": state.head_revision,
            "legacy_unmanaged": state.legacy_unmanaged,
        }
    )


DATA_GROUPS = {
    "Trader and learning": {
        "account_constraint_profiles",
        "trader_profiles",
        "learning_curricula",
        "learning_modules",
    },
    "Strategies and research": {
        "playbooks",
        "playbook_versions",
        "knowledge_imports",
        "strategy_knowledge_items",
        "strategy_experiments",
        "strategy_test_samples",
    },
    "Journal and decisions": {
        "trade_plans",
        "trade_reflections",
        "mindset_checkins",
        "pretrade_assessments",
        "rule_evaluations",
        "observations",
    },
    "Broker and execution ledger": {
        "trading_accounts",
        "broker_connections",
        "connector_cursors",
        "trades",
        "order_intents",
        "order_approvals",
        "execution_events",
        "fills",
        "trade_management_events",
        "position_snapshots",
        "account_snapshots",
    },
    "Market, news, and charts": {
        "instruments",
        "instrument_mappings",
        "instrument_specifications",
        "market_contexts",
        "economic_events",
        "news_items",
        "tradingview_alerts",
        "evidence_items",
        "analysis_runs",
    },
    "Agent conversations": {
        "conversation_sessions",
        "conversation_turns",
        "tool_execution_audits",
    },
}


@data_app.command("status")
def data_status() -> None:
    """Show a friendly inventory of stored trading data and its freshness."""
    upgrade_database()
    table_map = Base.metadata.tables
    with SessionLocal() as db:
        scope = _current_scope(db)
        table = Table(title="Trading Agent data inventory")
        table.add_column("Area")
        table.add_column("Rows", justify="right")
        table.add_column("Latest stored record")
        table.add_column("Included data")
        for group, names in DATA_GROUPS.items():
            present = [table_map[name] for name in sorted(names) if name in table_map]
            row_count = 0
            latest = None
            for item in present:
                timestamp = next(
                    (
                        item.c[name]
                        for name in (
                            "retrieved_at",
                            "received_at",
                            "updated_at",
                            "created_at",
                        )
                        if name in item.c
                    ),
                    None,
                )
                filters = []
                if "workspace_id" in item.c:
                    filters.append(item.c.workspace_id == scope.workspace_id)
                if "account_id" in item.c:
                    filters.append(item.c.account_id == scope.account_id)
                elif "trading_account_id" in item.c:
                    filters.append(item.c.trading_account_id == scope.account_id)
                elif item.name == "trading_accounts":
                    filters.append(item.c.id == scope.account_id)
                statement = select(
                    func.count(),
                    func.max(timestamp) if timestamp is not None else None,
                ).select_from(item)
                if filters:
                    statement = statement.where(*filters)
                count, candidate = db.execute(statement).one()
                row_count += int(count or 0)
                if candidate is not None and (latest is None or candidate > latest):
                    latest = candidate
            table.add_row(
                group,
                str(row_count),
                str(latest) if latest is not None else "No records yet",
                ", ".join(item.name for item in present),
            )
        console.print(table)
    console.print(
        "\n[dim]Live quotes and candles are intentionally not stored tick by tick. "
        "Use `trade broker sync`, `trade news sync`, `trade knowledge import`, or "
        "`trade chart --clipboard`; verified TradingView webhooks arrive "
        "automatically when enabled.[/dim]"
    )


@data_app.command("schema")
def data_schema() -> None:
    """Show every application table and its columns without exposing SQL access."""
    upgrade_database()
    table = Table(title="Trading Agent PostgreSQL schema")
    table.add_column("Table")
    table.add_column("Purpose")
    table.add_column("Columns")
    purpose_by_table = {name: group for group, names in DATA_GROUPS.items() for name in names}
    for item in Base.metadata.sorted_tables:
        columns = ", ".join(f"{column.name} ({column.type})" for column in item.columns)
        table.add_row(
            item.name,
            purpose_by_table.get(item.name, "Supporting data"),
            columns,
        )
    console.print(table)
    console.print(
        "\n[dim]The agent receives bounded application tools, not arbitrary SQL. "
        "That keeps credentials and unrelated private rows out of model context. "
        "See docs/data-model.md for relationships and retention rules.[/dim]"
    )


@database_app.command("adopt-legacy")
def database_adopt_legacy(
    backup: Annotated[
        Path,
        typer.Option(
            "--backup",
            help="New absolute path for a mandatory pg_dump backup.",
        ),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm the backup and transactional adoption."),
    ] = False,
) -> None:
    """Back up and transactionally adopt tables created before Alembic."""
    if not backup.is_absolute():
        console.print("[red]--backup must be an absolute path[/red]")
        raise typer.Exit(2)
    if not yes and not typer.confirm(
        "Back up the database, migrate the schema, verify copied rows, and remove legacy tables?"
    ):
        raise typer.Exit(1)
    try:
        adopt_legacy_database(backup)
    except (LegacySchemaDetectedError, FileExistsError, FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    current, head = schema_revisions()
    console.print(
        f"[green]Legacy schema adopted; backup={backup}; revision={current or head}[/green]"
    )


@broker_app.command("configure-oanda")
def broker_configure_oanda(
    label: Annotated[str, typer.Option(prompt=True)],
    currency: Annotated[str, typer.Option(prompt=True)] = "USD",
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Register the configured OANDA account without storing its token."""
    settings = get_settings()
    legacy = getattr(settings, "broker_secret_backend", "legacy-env") == "legacy-env"
    if legacy and (not settings.oanda_account_id or not settings.oanda_api_token):
        _render_broker_setup_error(
            settings,
            BrokerConfigurationError(
                "OANDA_API_TOKEN and OANDA_ACCOUNT_ID are required for OANDA reads"
            ),
            intended_provider="oanda",
        )
        raise typer.Exit(1)
    account_id = (
        secret_value(getattr(settings, "oanda_account_id", None))
        if legacy
        else typer.prompt("OANDA account ID").strip()
    )
    token = (
        secret_value(getattr(settings, "oanda_api_token", None))
        if legacy
        else typer.prompt("OANDA API token", hide_input=True).strip()
    )
    connector_settings = settings.model_copy(
        update={
            "oanda_account_id": SecretStr(account_id or ""),
            "oanda_api_token": SecretStr(token or ""),
        }
    ) if hasattr(settings, "model_copy") else settings
    connector = create_oanda_connector(connector_settings)

    async def verify():
        try:
            return await connector.account()
        finally:
            await connector.aclose()

    try:
        account_state = asyncio.run(verify())
    except (OandaConnectorError, ValueError) as exc:
        _render_broker_request_error(settings, exc, operation="verification")
        raise typer.Exit(1) from exc
    if account_state.external_account_id != account_id:
        console.print(
            "[red]OANDA returned a different account than OANDA_ACCOUNT_ID. "
            "Nothing was registered.[/red]"
        )
        raise typer.Exit(1)
    arguments = {
        "provider": "oanda-v20",
        "label": label,
        "currency": currency,
        "environment": settings.oanda_environment,
    }
    try:
        _authorize_direct(
            "configure_broker_connection",
            arguments,
            mutating=True,
            assume_yes=yes,
        )
    except PolicyViolation:
        _render_cancelled_mutation()
        raise typer.Exit(0) from None
    upgrade_database()
    with SessionLocal() as db:
        scope = _ensure_initial_scope(db, settings)
        account, connection = configure_account(
            db,
            workspace_id=scope.workspace_id,
            broker="OANDA",
            external_account_id=account_id,
            label=label,
            currency=currency,
            mode=settings.oanda_environment,
            provider="oanda-v20",
            environment=settings.oanda_environment,
            config_reference="env:OANDA_API_TOKEN" if legacy else None,
            make_default=True,
            commit=False,
        )
        workspace = resolve_workspace(db, scope.workspace_id)
        if workspace is None:
            raise LookupError("configured workspace was not found")
        config_path = default_config_path()
        env_snapshot = snapshot_env_file(config_path)
        try:
            update_env_file(
                config_path,
                {
                    "BROKER_PROVIDER": "oanda",
                    "TRADING_WORKSPACE": workspace.slug,
                    "TRADING_ACCOUNT": str(account.id),
                },
            )
            db.commit()
            if not legacy:
                rotate_broker_credential(
                    db,
                    settings,
                    scope=RequestScope(scope.workspace_id, account.id),
                    provider="oanda-v20",
                    token=token,
                    actor="local-cli",
                )
        except Exception:
            db.rollback()
            restore_env_file(config_path, env_snapshot)
            raise
        get_settings.cache_clear()
        _print_model(
            {
                "account_id": account.id,
                "connection_id": connection.id,
                "provider": connection.provider,
                "environment": connection.environment,
            }
        )


def _configured_oanda_connection(db) -> BrokerConnection:
    scope = _current_scope(db)
    statement = select(BrokerConnection).where(
        BrokerConnection.workspace_id == scope.workspace_id,
        BrokerConnection.account_id == scope.account_id,
        BrokerConnection.provider == "oanda-v20",
    )
    connections = list(db.scalars(statement))
    if len(connections) != 1:
        raise LookupError("run `trading-agent broker configure-oanda` for the configured account")
    return connections[0]


@broker_app.command("configure-metatrader")
def broker_configure_metatrader(
    label: Annotated[str, typer.Option(prompt=True)],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Verify and register one read-only MT4/MT5 bridge account."""
    settings = get_settings()
    legacy = getattr(settings, "broker_secret_backend", "legacy-env") == "legacy-env"
    token = (
        secret_value(getattr(settings, "metatrader_bridge_token", None))
        if legacy
        else typer.prompt("MetaTrader bridge token", hide_input=True).strip()
    )
    account_id = (
        secret_value(getattr(settings, "metatrader_account_id", None))
        if legacy
        else typer.prompt("MetaTrader account ID").strip()
    )
    connector_settings = settings.model_copy(
        update={
            "metatrader_bridge_token": SecretStr(token or ""),
            "metatrader_account_id": SecretStr(account_id or ""),
        }
    ) if hasattr(settings, "model_copy") else settings
    try:
        connector = create_metatrader_connector(connector_settings)
    except BrokerConfigurationError as exc:
        _render_broker_setup_error(
            settings,
            exc,
            intended_provider="metatrader",
        )
        raise typer.Exit(1) from exc

    async def verify():
        try:
            health = await connector.health()
            account = await connector.account()
            return health, account
        finally:
            await connector.aclose()

    try:
        health, account_state = asyncio.run(verify())
    except (MetaTraderBridgeError, ValueError) as exc:
        _render_broker_request_error(
            settings,
            exc,
            operation="verification",
        )
        raise typer.Exit(1) from exc
    arguments = {
        "provider": connector.name,
        "label": label,
        "currency": account_state.currency,
        "environment": settings.metatrader_mode,
        "platform": settings.metatrader_platform,
        "read_only": health["read_only"],
    }
    try:
        _authorize_direct(
            "configure_broker_connection",
            arguments,
            mutating=True,
            assume_yes=yes,
        )
    except PolicyViolation:
        _render_cancelled_mutation()
        raise typer.Exit(0) from None
    upgrade_database()
    with SessionLocal() as db:
        scope = _ensure_initial_scope(db, settings)
        account, connection = configure_account(
            db,
            workspace_id=scope.workspace_id,
            broker=settings.metatrader_platform.upper(),
            external_account_id=account_state.external_account_id,
            label=label,
            currency=account_state.currency,
            mode=settings.metatrader_mode,
            provider=connector.name,
            environment=settings.metatrader_mode,
            config_reference="env:METATRADER_BRIDGE_TOKEN" if legacy else None,
            make_default=True,
            commit=False,
        )
        workspace = resolve_workspace(db, scope.workspace_id)
        if workspace is None:
            raise LookupError("configured workspace was not found")
        config_path = default_config_path()
        env_snapshot = snapshot_env_file(config_path)
        try:
            update_env_file(
                config_path,
                {
                    "BROKER_PROVIDER": "metatrader",
                    "TRADING_WORKSPACE": workspace.slug,
                    "TRADING_ACCOUNT": str(account.id),
                },
            )
            db.commit()
            if not legacy:
                rotate_broker_credential(
                    db,
                    settings,
                    scope=RequestScope(scope.workspace_id, account.id),
                    provider=connector.name,
                    token=token,
                    actor="local-cli",
                )
        except Exception:
            db.rollback()
            restore_env_file(config_path, env_snapshot)
            raise
        get_settings.cache_clear()
        _print_model(
            {
                "account_id": account.id,
                "connection_id": connection.id,
                "provider": connection.provider,
                "environment": connection.environment,
                "terminal_connected": bool(health.get("terminal_connected")),
            }
        )


def _configured_broker_connection(db, settings: Settings) -> BrokerConnection:
    scope = _current_scope(db)
    if settings.broker_provider == "oanda":
        return _configured_oanda_connection(db)
    if settings.broker_provider == "metatrader":
        provider = f"metatrader-{settings.metatrader_platform}-bridge"
        statement = select(BrokerConnection).where(
            BrokerConnection.workspace_id == scope.workspace_id,
            BrokerConnection.account_id == scope.account_id,
            BrokerConnection.provider == provider,
        )
        matches = list(db.scalars(statement))
        if len(matches) != 1:
            raise LookupError(
                "run `trade broker configure-metatrader` for the configured account"
            )
        return matches[0]
    if settings.broker_provider in {"ibkr", "alpaca", "twelve-data", "ctrader"}:
        raise LookupError(
            "this broker provider is planned but not yet configured for live reads"
        )
    raise LookupError("select and configure a broker before synchronizing")


def _scoped_broker_connector(db, settings: Settings):
    connection = _configured_broker_connection(db, settings)
    return create_broker_connector(
        settings,
        account=connection.account,
        connection=connection,
    )


@broker_app.command("credential-rotate")
def broker_credential_rotate(
    provider: Annotated[
        str,
        typer.Option(help="Registered provider, such as oanda-v20."),
    ],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Create or rotate the selected account's vault-backed read credential."""
    settings = get_settings()
    if settings.broker_secret_backend == LEGACY_ENV_BACKEND:
        console.print(
            "[red]Credential rotation requires BROKER_SECRET_BACKEND=keyring "
            "or external.[/red]"
        )
        raise typer.Exit(2)
    secret = typer.prompt("Broker token", hide_input=True)
    arguments = {"provider": provider, "secret_backend": settings.broker_secret_backend}
    _authorize_direct(
        "rotate_broker_credential",
        arguments,
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    try:
        with SessionLocal() as db:
            scope = _current_scope(db)
            change = rotate_broker_credential(
                db,
                settings,
                scope=scope,
                provider=provider,
                token=secret,
                actor="local-cli",
            )
    except (LookupError, SecretBackendError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]Credential rotated for {change.connection.provider}; PostgreSQL stores "
        "only its opaque secret reference.[/green]"
    )
    if change.cleanup_pending:
        console.print(
            "[yellow]The new credential is active, but deletion of the previous "
            "vault entry failed. A retry-required security audit event was saved.[/yellow]"
        )


@broker_app.command("credential-remove")
def broker_credential_remove(
    provider: Annotated[str, typer.Option(help="Registered provider.")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Disable a connection and remove its credential from the secret backend."""
    settings = get_settings()
    _authorize_direct(
        "remove_broker_credential",
        {"provider": provider},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    try:
        with SessionLocal() as db:
            scope = _current_scope(db)
            change = remove_broker_credential(
                db,
                settings,
                scope=scope,
                provider=provider,
                actor="local-cli",
            )
    except (LookupError, SecretBackendError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]Credential removed and broker connection disabled.[/green]")
    if change.cleanup_pending:
        console.print(
            "[yellow]The connection is disabled, but vault deletion failed. Its "
            "reference was retained and a retry-required audit event was saved.[/yellow]"
        )


@broker_app.command("credential-cleanup-retry")
def broker_credential_cleanup_retry(
    audit_event_id: uuid.UUID,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Retry vault cleanup recorded by one selected-account security audit event."""
    settings = get_settings()
    _authorize_direct(
        "retry_broker_credential_cleanup",
        {"audit_event_id": str(audit_event_id)},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    try:
        with SessionLocal() as db:
            retry_broker_secret_cleanup(
                db,
                settings,
                scope=_current_scope(db),
                audit_event_id=audit_event_id,
                actor="local-cli",
            )
    except (LookupError, RuntimeError, SecretBackendError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]Vault cleanup retry succeeded and was audited.[/green]")


@principal_app.command("create")
def principal_create(
    subject: Annotated[str, typer.Argument(help="Stable user/service subject.")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Create a hosted identity and display its bearer token exactly once."""
    _authorize_direct(
        "create_api_principal",
        {"subject": subject},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    try:
        with SessionLocal() as db:
            principal, token = create_principal(db, subject)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"Principal: {principal.id}")
    console.print(f"Bearer token (shown once): {token}")


@principal_app.command("grant")
def principal_grant(
    principal_id: uuid.UUID,
    role: Annotated[
        str,
        typer.Option(help="reader, trader, or admin"),
    ] = "reader",
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Grant one principal access to only the currently selected account."""
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        principal = db.get(ApiPrincipal, principal_id)
        if principal is None:
            console.print("[red]Principal was not found.[/red]")
            raise typer.Exit(1)
        _authorize_direct(
            "grant_api_principal",
            {
                "principal_id": str(principal_id),
                "workspace_id": str(scope.workspace_id),
                "account_id": str(scope.account_id),
                "role": role,
            },
            mutating=True,
            assume_yes=yes,
        )
        try:
            grant_principal(
                db,
                principal_id=principal_id,
                scope=scope,
                role=role,
                actor="local-cli",
            )
        except (LookupError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    console.print("[green]Principal grant saved for the selected account only.[/green]")


@principal_app.command("rotate-token")
def principal_rotate_token(
    principal_id: uuid.UUID,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Invalidate a principal's old bearer token and display the replacement once."""
    _authorize_direct(
        "rotate_api_principal_token",
        {"principal_id": str(principal_id)},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    try:
        with SessionLocal() as db:
            token = rotate_principal_token(
                db,
                principal_id,
                scope=_current_scope(db),
                actor="local-cli",
            )
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"Replacement bearer token (shown once): {token}")


@principal_app.command("revoke")
def principal_revoke(
    principal_id: uuid.UUID,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Revoke a principal's grant to the currently selected account."""
    _authorize_direct(
        "revoke_api_principal",
        {"principal_id": str(principal_id)},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    try:
        with SessionLocal() as db:
            revoke_principal_grant(
                db,
                principal_id=principal_id,
                scope=_current_scope(db),
                actor="local-cli",
            )
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]Principal grant revoked for the selected account.[/green]")


@broker_app.command("quote")
def broker_quote(instrument: str) -> None:
    """Read one live broker quote with market and retrieval timestamps."""
    settings = get_settings()
    upgrade_database()
    try:
        with SessionLocal() as db:
            connector = _scoped_broker_connector(db, settings)
    except (BrokerConfigurationError, LookupError) as exc:
        _render_broker_setup_error(settings, exc)
        raise typer.Exit(1) from exc
    _authorize_direct("get_live_quote", {"instrument": instrument})

    async def read():
        try:
            return await connector.latest_quote(instrument)
        finally:
            await connector.aclose()

    try:
        quote = asyncio.run(read())
    except (MetaTraderBridgeError, OandaConnectorError, ValueError) as exc:
        _render_broker_request_error(settings, exc, operation="quote")
        raise typer.Exit(1) from exc
    _print_model(quote)


@broker_app.command("sync")
def broker_sync(
    from_cursor: Annotated[
        str | None,
        typer.Option(
            "--from-cursor",
            "--from-transaction-id",
            help=(
                "One-time history start cursor. OANDA uses a transaction id; "
                "MetaTrader accepts the bridge cursor or an ISO-8601 timestamp."
            ),
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Import new broker executions and reconcile account/position snapshots."""
    settings = get_settings()
    connector = None
    if settings.broker_provider == "none":
        try:
            create_broker_connector(settings)
        except BrokerConfigurationError as exc:
            _render_broker_setup_error(settings, exc)
            raise typer.Exit(1) from exc
    try:
        upgrade_database()
        with SessionLocal() as db:
            scope = _current_scope(db)
            connection = _configured_broker_connection(db, settings)
            connector = create_broker_connector(
                settings,
                account=connection.account,
                connection=connection,
            )
            if from_cursor is not None:
                if settings.broker_provider == "oanda" and not from_cursor.isdigit():
                    raise ValueError("OANDA --from-cursor must contain only digits")
                existing_cursor = db.scalar(
                    select(ConnectorCursor).where(
                        ConnectorCursor.workspace_id == scope.workspace_id,
                        ConnectorCursor.account_id == scope.account_id,
                        ConnectorCursor.connection_id == connection.id,
                        ConnectorCursor.stream_name == "transactions",
                    )
                )
                if existing_cursor is not None:
                    raise ValueError("a transaction cursor already exists; refusing to rewind it")

            try:
                _authorize_direct(
                    "synchronize_broker",
                    {
                        "provider": connector.name,
                        "from_cursor": from_cursor,
                    },
                    mutating=True,
                    assume_yes=yes,
                )
            except PolicyViolation:
                _render_cancelled_mutation()
                raise typer.Exit(0) from None

            if from_cursor is not None:
                db.add(
                    ConnectorCursor(
                        workspace_id=scope.workspace_id,
                        account_id=scope.account_id,
                        connection_id=connection.id,
                        stream_name="transactions",
                        cursor_value=from_cursor,
                    )
                )
                db.flush()

            async def synchronize():
                return await synchronize_broker(
                    db,
                    scope=scope,
                    connection_id=connection.id,
                    connector=connector,
                )

            _print_model(asyncio.run(synchronize()))
    except typer.Exit:
        raise
    except (LookupError, ValueError) as exc:
        console.print(f"[red]{escape_markup(str(exc))}[/red]")
        console.print("[dim]Nothing was changed.[/dim]")
        raise typer.Exit(1) from exc
    except (MetaTraderBridgeError, OandaConnectorError) as exc:
        _render_broker_request_error(settings, exc, operation="synchronization")
        raise typer.Exit(1) from exc
    finally:
        if connector is not None:
            asyncio.run(connector.aclose())


@edge_app.command("report")
def edge_report(
    minimum_sample: Annotated[int, typer.Option(min=5, max=1000)] = 30,
) -> None:
    """Report expectancy only by stable setup/instrument/regime/timeframe segments."""
    _authorize_direct("build_edge_report", {"minimum_sample": minimum_sample})
    upgrade_database()
    with SessionLocal() as db:
        _print_model(
            build_edge_report(db, minimum_sample, scope=_current_scope(db))
        )


@playbook_app.command("version")
def playbook_version(
    name: Annotated[str, typer.Option()],
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    hypothesis: Annotated[str | None, typer.Option()] = None,
    minimum_sample: Annotated[int | None, typer.Option(min=5)] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Freeze a measurable playbook definition as a new immutable version."""
    try:
        definition = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    try:
        definition = canonical_strategy_definition(
            definition,
            maximum_risk_percent=Decimal(str(get_settings().maximum_trade_risk_percent)),
        )
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]Invalid strategy definition: {exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "create_playbook_version",
        {
            "name": name,
            "definition": definition,
            "change_hypothesis": hypothesis,
            "minimum_sample": minimum_sample,
        },
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        version = create_validated_strategy_version(
            db,
            scope=_current_scope(db),
            name=name,
            definition=definition,
            maximum_risk_percent=Decimal(str(get_settings().maximum_trade_risk_percent)),
            change_hypothesis=hypothesis,
            sample_requirement=minimum_sample,
        )
        _print_model(
            {
                "id": version.id,
                "version": version.version,
                "content_hash": version.content_hash,
            }
        )


@strategy_app.command("create")
def strategy_create(
    name: Annotated[str, typer.Option(help="Unique strategy name, such as wyckoff.")],
    file: Annotated[
        Path,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="JSON definition with explicit rules and exclusions.",
        ),
    ],
    description: Annotated[str, typer.Option()] = "",
    hypothesis: Annotated[str | None, typer.Option()] = None,
    minimum_sample: Annotated[int, typer.Option(min=5)] = 30,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Create an immutable, isolated strategy version."""
    try:
        definition = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    try:
        definition = canonical_strategy_definition(
            definition,
            maximum_risk_percent=Decimal(str(get_settings().maximum_trade_risk_percent)),
        )
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]Invalid strategy definition: {exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "create_strategy_version",
        {
            "name": name,
            "definition": definition,
            "description": description,
            "hypothesis": hypothesis,
            "minimum_sample": minimum_sample,
        },
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        version = create_validated_strategy_version(
            db,
            scope=_current_scope(db),
            name=name,
            definition=definition,
            maximum_risk_percent=Decimal(str(get_settings().maximum_trade_risk_percent)),
            description=description,
            change_hypothesis=hypothesis,
            sample_requirement=minimum_sample,
        )
        _print_model(
            {
                "strategy": name,
                "version": version.version,
                "playbook_version_id": version.id,
                "content_hash": version.content_hash,
                "isolation": (
                    "Only knowledge imported into this exact version is retrievable. "
                    "Create a separate explicit combined strategy to mix methodologies."
                ),
            }
        )


@strategy_app.command("list")
def strategy_list() -> None:
    """List latest immutable strategy versions and scoped knowledge counts."""
    upgrade_database()
    with SessionLocal() as db:
        _print_model(
            [
                item.model_dump(mode="json")
                for item in list_strategy_summaries(db, scope=_current_scope(db))
            ]
        )


@strategy_app.command("use")
def strategy_use(
    name: Annotated[str, typer.Argument(help="Strategy name to isolate in the session.")],
    session: Annotated[str | None, typer.Option(help="Session name or UUID.")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Select exactly one strategy version for a conversation."""
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        conversation = (
            resolve_conversation(db, session, scope=scope)
            if session
            else latest_conversation(db, scope=scope)
        )
        if conversation is None:
            console.print("[red]No conversation session exists yet.[/red]")
            raise typer.Exit(1)
        try:
            proposed_playbook, proposed_version = resolve_strategy_version(
                db,
                name,
                scope=scope,
            )
            _authorize_direct(
                "set_session_strategy",
                {
                    "session": conversation.name,
                    "strategy": proposed_playbook.name,
                    "version": proposed_version.version,
                    "content_hash": proposed_version.content_hash,
                },
                mutating=True,
                assume_yes=yes,
            )
            playbook, version = set_session_strategy(
                db,
                conversation,
                proposed_playbook.name,
                scope=scope,
                version=proposed_version.version,
            )
        except (LookupError, PolicyViolation) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(
            f"[green]Session {conversation.name} now uses only "
            f"{playbook.name} v{version.version} ({version.content_hash[:12]}).[/green]"
        )


@strategy_app.command("clear")
def strategy_clear(
    session: Annotated[str | None, typer.Option(help="Session name or UUID.")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Clear strategy-specific retrieval for a conversation."""
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        conversation = (
            resolve_conversation(db, session, scope=scope)
            if session
            else latest_conversation(db, scope=scope)
        )
        if conversation is None:
            console.print("[red]No conversation session exists yet.[/red]")
            raise typer.Exit(1)
        previous = active_session_strategy(db, conversation, scope=scope)
        _authorize_direct(
            "clear_session_strategy",
            {
                "session": conversation.name,
                "previous_strategy": (
                    None
                    if previous is None
                    else {
                        "name": previous[0].name,
                        "version": previous[1].version,
                        "content_hash": previous[1].content_hash,
                    }
                ),
            },
            mutating=True,
            assume_yes=yes,
        )
        set_session_strategy(db, conversation, None, scope=scope)
        console.print(f"[green]Session {conversation.name} has no active strategy context.[/green]")


@knowledge_app.command("import")
def knowledge_import_command(
    path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            help="TXT, Markdown, JSON, JSONL, CSV, JS, Discord ZIP, or directory.",
        ),
    ],
    strategy: Annotated[
        str,
        typer.Option(help="Exact isolated strategy receiving this material."),
    ],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Import and index external trading material without changing model weights."""
    _authorize_direct(
        "import_strategy_knowledge",
        {"path": str(path.resolve()), "strategy": strategy},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            result = import_knowledge_path(
                db,
                path,
                strategy,
                scope=_current_scope(db),
            )
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            LookupError,
            json.JSONDecodeError,
        ) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        _print_model(result)


@knowledge_app.command("paste")
def knowledge_paste_command(
    strategy: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()] = "pasted-notes",
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Paste one note interactively into an isolated strategy."""
    text = typer.prompt("Knowledge text")
    _authorize_direct(
        "import_strategy_knowledge",
        {"source": "paste", "strategy": strategy, "name": name},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            _print_model(
                import_knowledge_text(
                    db,
                    text,
                    strategy,
                    name,
                    scope=_current_scope(db),
                )
            )
        except (ValueError, LookupError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


@knowledge_app.command("search")
def knowledge_search_command(
    strategy: Annotated[str, typer.Option()],
    query: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option(min=1, max=25)] = 8,
) -> None:
    """Search only one strategy version's indexed knowledge."""
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        try:
            playbook, version = resolve_strategy_version(
                db,
                strategy,
                scope=scope,
            )
            items = search_strategy_knowledge(
                db,
                version.id,
                query,
                limit,
                scope=scope,
            )
        except (ValueError, LookupError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        _print_model(
            {
                "strategy": playbook.name,
                "version": version.version,
                "results": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "source_reference": item.source_reference,
                        "occurred_at": item.occurred_at,
                        "content": item.content,
                        "content_hash": item.content_hash,
                    }
                    for item in items
                ],
            }
        )


def _set_knowledge_excluded(
    item_id: uuid.UUID,
    strategy: str,
    *,
    excluded: bool,
    yes: bool,
) -> None:
    action = "exclude_strategy_knowledge" if excluded else "restore_strategy_knowledge"
    _authorize_direct(
        action,
        {"item_id": str(item_id), "strategy": strategy},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            item = set_strategy_knowledge_excluded(
                db,
                strategy,
                item_id,
                scope=_current_scope(db),
                excluded=excluded,
            )
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    status = "excluded from retrieval" if excluded else "restored to retrieval"
    console.print(f"[green]{item.id} is {status} for {strategy}.[/green]")


@knowledge_app.command("exclude")
def knowledge_exclude_command(
    item_id: Annotated[uuid.UUID, typer.Argument(help="Knowledge item UUID.")],
    strategy: Annotated[str, typer.Option(help="Exact strategy version scope.")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Quarantine one item from strategy retrieval without deleting evidence."""
    _set_knowledge_excluded(item_id, strategy, excluded=True, yes=yes)


@knowledge_app.command("restore")
def knowledge_restore_command(
    item_id: Annotated[uuid.UUID, typer.Argument(help="Knowledge item UUID.")],
    strategy: Annotated[str, typer.Option(help="Exact strategy version scope.")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Restore one quarantined item to its exact strategy version."""
    _set_knowledge_excluded(item_id, strategy, excluded=False, yes=yes)


@experiment_app.command("start")
def experiment_start(
    strategy: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()],
    mode: Annotated[str, typer.Option(help="backtest or forward_test")],
    hypothesis: Annotated[str, typer.Option()],
    instrument: Annotated[str | None, typer.Option()] = None,
    timeframe: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Start a test frozen to one exact strategy definition hash."""
    try:
        request = StrategyExperimentCreate(
            strategy=strategy,
            name=name,
            mode=mode,
            hypothesis=hypothesis,
            instrument=instrument,
            timeframe=timeframe,
        )
    except ValidationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "create_strategy_experiment",
        request.model_dump(mode="json"),
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            _print_model(
                StrategyExperimentRead.model_validate(
                    create_strategy_experiment(
                        db,
                        request,
                        scope=_current_scope(db),
                    )
                )
            )
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


@experiment_app.command("sample")
def experiment_sample(
    experiment_id: str,
    file: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, help="StrategyTestSampleCreate JSON."),
    ],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Add one eligible, excluded, or unclear test observation."""
    try:
        request = StrategyTestSampleCreate.model_validate_json(file.read_text())
    except (OSError, ValidationError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "add_strategy_test_sample",
        {
            "experiment_id": str(experiment_id),
            **request.model_dump(mode="json"),
        },
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            sample = add_strategy_test_sample(
                db,
                experiment_id,
                request,
                scope=_current_scope(db),
            )
            _print_model(
                {
                    "id": sample.id,
                    "experiment_id": sample.experiment_id,
                    "classification": sample.classification,
                    "outcome_r": sample.outcome_r,
                    "feature_snapshot": sample.feature_snapshot,
                    "created_at": sample.created_at,
                }
            )
        except (ValueError, LookupError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


@experiment_app.command("correlations")
def experiment_correlations(
    experiment_id: str,
    minimum_samples: Annotated[int, typer.Option(min=5, max=1000)] = 10,
) -> None:
    """Measure descriptive feature/outcome correlations for one isolated test."""
    upgrade_database()
    with SessionLocal() as db:
        try:
            experiment = resolve_strategy_experiment(
                db,
                experiment_id,
                scope=_current_scope(db),
            )
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(
            experiment_feature_correlations(
                db,
                experiment.id,
                scope=_current_scope(db),
                minimum_samples=minimum_samples,
            )
        )


@experiment_app.command("report")
def experiment_report(experiment_id: str) -> None:
    """Show sample counts, exclusions, expectancy, and feature correlations."""
    upgrade_database()
    with SessionLocal() as db:
        try:
            _print_model(
                strategy_experiment_report(
                    db,
                    experiment_id,
                    scope=_current_scope(db),
                )
            )
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc


@experiment_app.command("complete")
def experiment_complete(
    experiment_id: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Freeze a running backtest or forward test."""
    _authorize_direct(
        "complete_strategy_experiment",
        {"experiment_id": str(experiment_id)},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            _print_model(
                StrategyExperimentRead.model_validate(
                    complete_strategy_experiment(
                        db,
                        experiment_id,
                        scope=_current_scope(db),
                    )
                )
            )
        except (ValueError, LookupError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


@experiment_app.command("show")
def experiment_show(experiment_id: str) -> None:
    """Show an experiment and its frozen strategy hash."""
    upgrade_database()
    with SessionLocal() as db:
        try:
            experiment = resolve_strategy_experiment(
                db,
                experiment_id,
                scope=_current_scope(db),
            )
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(StrategyExperimentRead.model_validate(experiment))


@news_app.command("sync")
def news_sync(
    start: Annotated[str, typer.Option()],
    end: Annotated[str, typer.Option()],
    countries: Annotated[str, typer.Option()] = "United States",
    news_country: Annotated[str | None, typer.Option()] = "United States",
    minimum_importance: Annotated[int, typer.Option(min=0, max=3)] = 2,
    news_limit: Annotated[int, typer.Option(min=1, max=250)] = 50,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Fetch and idempotently retain event/headline metadata, not article bodies."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        connector = create_news_connector(get_settings())
    except (ValueError, BrokerConfigurationError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    country_values = [value.strip() for value in countries.split(",") if value.strip()]
    _authorize_direct(
        "synchronize_news",
        {
            "start": start,
            "end": end,
            "countries": country_values,
            "news_country": news_country,
            "minimum_importance": minimum_importance,
            "news_limit": news_limit,
        },
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()

    async def fetch():
        try:
            calendar = await connector.calendar(
                start=start_date,
                end=end_date,
                countries=country_values,
                minimum_importance=minimum_importance,
            )
            headlines = await connector.news(
                country=news_country,
                limit=news_limit,
            )
            return calendar, headlines
        finally:
            await connector.aclose()

    try:
        calendar, headlines = asyncio.run(fetch())
    except RuntimeError as exc:
        console.print("[bold red]News sync unavailable[/bold red]")
        console.print(str(exc))
        console.print(
            "[dim]Previously stored calendar data remains available. "
            "Wait for the provider's retry window, then run this command again.[/dim]"
        )
        raise typer.Exit(1) from None
    with SessionLocal() as db:
        calendar_count = store_calendar_events(db, tuple(calendar))
        news_count = store_news_items(db, tuple(headlines))
    provider_name = get_settings().news_provider.replace("-", " ").title()
    console.print("[bold green]✓ News sync complete[/bold green]")
    console.print(f"[bold]Source[/bold]  {provider_name}")
    console.print(
        f"[bold]Calendar[/bold]  {len(calendar)} received · {calendar_count} new"
    )
    if headlines:
        console.print(
            f"[bold]Headlines[/bold] {len(headlines)} received · {news_count} new"
        )
    elif get_settings().news_provider == "forex-factory":
        console.print(
            "[dim]Forex Factory supplies calendar events, not a headline API.[/dim]"
        )
    if not calendar and not headlines:
        console.print(
            "[yellow]No matching items were found for this date, currency, "
            "and impact filter.[/yellow]"
        )


@news_app.command("upcoming")
def news_upcoming(
    hours: Annotated[int, typer.Option(min=1, max=168)] = 24,
    currencies: Annotated[str, typer.Option()] = "USD",
    minimum_importance: Annotated[int, typer.Option(min=0, max=3)] = 2,
    details: Annotated[bool, typer.Option("--details")] = False,
) -> None:
    """Show concise upcoming events from the stored calendar."""
    currency_values = tuple(
        dict.fromkeys(
            value.strip().upper()
            for value in currencies.split(",")
            if value.strip()
        )
    )
    now = datetime.now(UTC)
    through = now + timedelta(hours=hours)
    upgrade_database()
    with SessionLocal() as db:
        statement = (
            select(EconomicEvent)
            .where(
                EconomicEvent.scheduled_at >= now,
                EconomicEvent.scheduled_at <= through,
                EconomicEvent.importance >= minimum_importance,
            )
            .order_by(EconomicEvent.scheduled_at, EconomicEvent.importance.desc())
        )
        if currency_values:
            statement = statement.where(
                EconomicEvent.currency.in_(currency_values)
            )
        events = tuple(db.scalars(statement))

    console.print("[bold]Trading Agent: Upcoming economic events[/bold]")
    if not events:
        console.print(
            "[yellow]No stored events match this window and filter.[/yellow]"
        )
        console.print(
            "[dim]Run `trade news sync` to refresh the calendar, then try again.[/dim]"
        )
        return
    local_timezone = datetime.now().astimezone().tzinfo
    table = Table(show_header=True, box=None, pad_edge=False)
    table.add_column("Time", no_wrap=True)
    table.add_column("Currency", no_wrap=True)
    table.add_column("Impact", no_wrap=True)
    table.add_column("Event")
    impact_names = {0: "Info", 1: "Low", 2: "Medium", 3: "High"}
    for event in events:
        local_time = event.scheduled_at.astimezone(local_timezone)
        table.add_row(
            local_time.strftime("%a %H:%M %Z"),
            event.currency or "—",
            impact_names[event.importance],
            event.title,
        )
    console.print(table)
    console.print(
        f"[dim]{len(events)} event(s) through "
        f"{through.astimezone(local_timezone).strftime('%a %H:%M %Z')} · "
        "stored provider evidence, not trading instructions[/dim]"
    )
    if details:
        for event in events:
            insight = event_insight(event.title, event.currency)
            local_time = event.scheduled_at.astimezone(local_timezone)
            console.print()
            console.rule(f"[bold]{event.title}[/bold]", style="dim")
            console.print(
                f"[dim]{local_time.strftime('%A, %H:%M %Z')} · "
                f"{event.currency or '—'} · "
                f"{impact_names[event.importance]} impact[/dim]"
            )
            console.print()
            values = Table(show_header=True, box=None, pad_edge=False)
            values.add_column("Actual", min_width=12)
            values.add_column("Forecast", min_width=12)
            values.add_column("Previous", min_width=12)
            values.add_row(
                f"[bold]{event.actual or 'Pending'}[/bold]",
                event.forecast or "—",
                event.previous or "—",
            )
            console.print(values)
            console.print()
            console.print("[bold]What it measures[/bold]")
            console.print(insight.measures)
            console.print()
            console.print("[bold]Why markets watch it[/bold]")
            console.print(insight.why_markets_watch)
            console.print()
            if insight.sensitive_markets:
                console.print("[bold]Commonly sensitive markets[/bold]")
                console.print(" · ".join(insight.sensitive_markets))
                console.print()
            console.print("[bold yellow]Interpret carefully[/bold yellow]")
            console.print(f"[dim]{insight.interpretation_caution}[/dim]")
            if insight.source_label and insight.source_url:
                console.print()
                console.print("[bold]Primary reference[/bold]")
                console.print(insight.source_label)
                console.print(
                    f"[link={insight.source_url}]{insight.source_url}[/link]"
                )


@news_app.command("history")
def news_history(
    event: Annotated[str, typer.Argument(help="Event name, such as Core PCE or GDP.")],
    currency: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=50)] = 10,
) -> None:
    """Show stored past observations for one requested economic event."""
    upgrade_database()
    with SessionLocal() as db:
        try:
            events = economic_event_history(
                db,
                event,
                currency=currency,
                limit=limit,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

    console.print(f"[bold]Trading Agent: Previous {event.strip()} releases[/bold]")
    if not events:
        console.print("[yellow]No matching past releases are stored yet.[/yellow]")
        console.print(
            "[dim]The free weekly feed builds history as calendar syncs are retained; "
            "it is not a complete historical archive.[/dim]"
        )
        return

    local_timezone = datetime.now().astimezone().tzinfo
    impact_names = {0: "Info", 1: "Low", 2: "Medium", 3: "High"}
    table = Table(show_header=True, box=None, pad_edge=False)
    table.add_column("Date", no_wrap=True)
    table.add_column("Event")
    table.add_column("Impact", no_wrap=True)
    table.add_column("Actual", no_wrap=True)
    table.add_column("Forecast", no_wrap=True)
    table.add_column("Previous", no_wrap=True)
    for item in events:
        table.add_row(
            item.scheduled_at.astimezone(local_timezone).strftime("%Y-%m-%d %H:%M %Z"),
            item.title,
            impact_names[item.importance],
            item.actual or "—",
            item.forecast or "—",
            item.previous or "—",
        )
    console.print(table)
    console.print(
        f"[dim]{len(events)} stored release(s) · "
        "values are provider evidence, not a directional signal[/dim]"
    )


@news_app.command("watch")
def news_watch(
    interval_seconds: Annotated[int, typer.Option(min=30, max=3600)] = 300,
    alert_minutes: Annotated[int, typer.Option(min=1, max=1440)] = 60,
    currencies: Annotated[str, typer.Option()] = "USD",
    minimum_importance: Annotated[int, typer.Option(min=0, max=3)] = 2,
    once: Annotated[bool, typer.Option("--once")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Refresh the calendar on a schedule and print newly due event alerts."""
    settings = get_settings()
    if not news_provider_configured(settings):
        console.print(
            "[red]Select a configured news provider before starting calendar watch.[/red]"
        )
        raise typer.Exit(2)
    currency_values = tuple(
        dict.fromkeys(
            value.strip().upper()
            for value in currencies.split(",")
            if value.strip()
        )
    )
    _authorize_direct(
        "synchronize_news",
        {
            "mode": "watch",
            "interval_seconds": interval_seconds,
            "alert_minutes": alert_minutes,
            "currencies": currency_values,
            "minimum_importance": minimum_importance,
        },
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    console.print("[bold]Trading Agent: Economic calendar watch[/bold]")
    console.print(
        f"Refreshing every {interval_seconds}s · alert window {alert_minutes}m · "
        f"currencies {', '.join(currency_values) or 'all'}"
    )
    console.print("[dim]Press Ctrl-C to stop. No orders can be placed.[/dim]")
    notified: set[tuple[str, str]] = set()

    async def refresh():
        connector = create_news_connector(settings)
        try:
            today = datetime.now(UTC).date()
            return await connector.calendar(
                start=today,
                end=today + timedelta(days=settings.startup_news_horizon_days),
                countries=currency_values,
                minimum_importance=minimum_importance,
            )
        finally:
            await connector.aclose()

    try:
        while True:
            try:
                fetched = tuple(asyncio.run(refresh()))
            except RuntimeError as exc:
                console.print(
                    f"[yellow]Calendar refresh unavailable: {exc}. "
                    "Using stored events.[/yellow]"
                )
            else:
                with SessionLocal() as db:
                    added = store_calendar_events(db, fetched)
                console.print(
                    f"[dim]{datetime.now().astimezone().strftime('%H:%M:%S %Z')} · "
                    f"{len(fetched)} received · {added} new[/dim]"
                )

            now = datetime.now(UTC)
            through = now + timedelta(minutes=alert_minutes)
            with SessionLocal() as db:
                statement = (
                    select(EconomicEvent)
                    .where(
                        EconomicEvent.scheduled_at >= now,
                        EconomicEvent.scheduled_at <= through,
                        EconomicEvent.importance >= minimum_importance,
                    )
                    .order_by(
                        EconomicEvent.scheduled_at,
                        EconomicEvent.importance.desc(),
                    )
                )
                if currency_values:
                    statement = statement.where(
                        EconomicEvent.currency.in_(currency_values)
                    )
                due = tuple(db.scalars(statement))
            new_due = tuple(
                event
                for event in due
                if (event.source, event.source_event_id) not in notified
            )
            for event in new_due:
                local_time = event.scheduled_at.astimezone()
                console.print()
                console.print(
                    f"[bold yellow]Economic event approaching · "
                    f"{event.currency or '—'} · "
                    f"{local_time.strftime('%H:%M %Z')}[/bold yellow]"
                )
                console.print(event.title)
                console.print(
                    f"[dim]Impact {event.importance}/3 · source {event.source} · "
                    "untrusted calendar evidence[/dim]"
                )
                notified.add((event.source, event.source_event_id))
            if once:
                return
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        console.print("\n[dim]Calendar watch stopped.[/dim]")


@sessions_app.command("show")
def sessions_show(session: str) -> None:
    """Show the saved transcript for one session."""
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        conversation: ConversationSession | None = resolve_conversation(
            db,
            session,
            scope=scope,
        )
        if conversation is None:
            console.print(f"[red]Conversation {session} was not found.[/red]")
            raise typer.Exit(1)
        for turn in conversation_transcript(
            db,
            conversation,
            scope=scope,
            limit=100,
        ):
            console.print(Panel(turn["content"], title=turn["role"]))


@app.command()
def review(
    trade_id: str,
    exit_average: Annotated[str, typer.Option(prompt=True)],
    realized_pnl: Annotated[str, typer.Option(prompt=True)],
    execution_grade: Annotated[str, typer.Option(prompt=True)],
    notes: Annotated[str, typer.Option(prompt=True)] = "",
    yes: Annotated[bool, typer.Option("--yes", help="Save without the final prompt.")] = False,
) -> None:
    """Add a post-trade reflection."""
    try:
        request = ReflectionCreate(
            exit_average=exit_average,
            realized_pnl=realized_pnl,
            execution_grade=execution_grade,
            notes=notes,
        )
    except ValidationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "add_trade_reflection",
        {"trade_id": str(trade_id), **request.model_dump(mode="json")},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            reflection = create_reflection(
                db,
                trade_id,
                request,
                scope=_current_scope(db),
            )
        except (TradeNotFoundError, ReflectionExistsError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(ReflectionRead.model_validate(reflection))


@app.command("manage")
def manage_trade(
    trade_id: uuid.UUID,
    event_type: Annotated[str, typer.Option(prompt=True)],
    reason: Annotated[str, typer.Option(prompt=True)],
    occurred_at: Annotated[str, typer.Option(prompt=True)],
    price: Annotated[str | None, typer.Option()] = None,
    quantity_delta: Annotated[str | None, typer.Option()] = None,
    position_quantity_after: Annotated[str | None, typer.Option()] = None,
    realized_r: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Append a partial, runner, hedge consideration, or other management decision."""
    try:
        request = ManagementEventCreate(
            event_type=event_type,
            reason=reason,
            occurred_at=occurred_at,
            price=price,
            quantity_delta=quantity_delta,
            position_quantity_after=position_quantity_after,
            realized_r_at_event=realized_r,
        )
    except ValidationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _authorize_direct(
        "record_management_event",
        {"trade_id": str(trade_id), **request.model_dump(mode="json")},
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        try:
            event = record_management_event(
                db,
                trade_id,
                request,
                scope=_current_scope(db),
            )
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(
            {
                "id": event.id,
                "trade_id": event.trade_id,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
            }
        )


@app.command()
def chart(
    image: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="PNG, JPEG, or WebP chart path. Omit when using --clipboard.",
        ),
    ] = None,
    clipboard: Annotated[
        bool,
        typer.Option(
            "--clipboard",
            help="Read an image directly from the system clipboard; clipboard text is ignored.",
        ),
    ] = False,
    context: Annotated[
        str,
        typer.Option(help="Known context; never inferred from the image."),
    ] = "",
    instrument: Annotated[str | None, typer.Option()] = None,
    venue: Annotated[str | None, typer.Option()] = None,
    timeframe: Annotated[str | None, typer.Option()] = None,
    market_time: Annotated[
        str | None,
        typer.Option(help="Timezone-aware ISO market time; omit when unknown."),
    ] = None,
    trade_plan: Annotated[
        str | None,
        typer.Option(
            "--trade-plan",
            "--trade-plan-id",
            help="Human trade reference or internal UUID.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="One-call model override; useful for local vision models."),
    ] = None,
    reasoning_effort: Annotated[
        str,
        typer.Option(help="low, medium, or high."),
    ] = "medium",
) -> None:
    """Analyze a chart screenshot from a path or the system clipboard."""
    if clipboard == (image is not None):
        console.print(
            "[red]Choose exactly one chart source: provide an image path or use "
            "`--clipboard`.[/red]"
        )
        raise typer.Exit(2)
    source_label = "clipboard" if clipboard else str(image)
    authorization_source = {"clipboard": True} if clipboard else {"image_path": str(image)}
    _authorize_direct(
        "analyze_chart",
        {
            **authorization_source,
            "context": context,
            "instrument": instrument,
            "venue": venue,
            "timeframe": timeframe,
            "market_time": market_time,
            "trade_plan": trade_plan,
            "model": model,
            "reasoning_effort": reasoning_effort,
        },
        mutating=True,
    )
    settings = get_settings()
    resolved_image: Path | None = None
    if clipboard:
        try:
            clipboard_image = read_clipboard_image()
        except ClipboardImageError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        image_bytes = clipboard_image.data
        content_type = clipboard_image.content_type
        source_label = clipboard_image.source
    else:
        if image is None:
            raise typer.BadParameter("provide --image or use --clipboard")
        try:
            resolved_image, image_bytes = _read_approved_chart(
                str(image),
                user_message=str(image),
                settings=settings,
                additional_roots=(image.expanduser().absolute().parent,),
            )
        except (OSError, PermissionError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        content_type, _ = mimetypes.guess_type(resolved_image)
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        console.print("[red]Chart must be PNG, JPEG, or WebP.[/red]")
        raise typer.Exit(2)
    observed_at = None
    if market_time is not None:
        try:
            observed_at = datetime.fromisoformat(market_time.replace("Z", "+00:00"))
        except ValueError as exc:
            console.print("[red]--market-time must be ISO-8601[/red]")
            raise typer.Exit(2) from exc
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            console.print("[red]--market-time must include a timezone[/red]")
            raise typer.Exit(2)
    provider = create_model_provider(settings)
    destination = _chart_destination(settings, provider)
    disclosure = {
        "provider": provider.name,
        "destination": destination,
        "content_type": content_type,
        "image_bytes": len(image_bytes),
        "context": context,
    }
    if resolved_image is not None:
        disclosure["image_path"] = str(resolved_image)
    else:
        disclosure["image_source"] = source_label
    if destination is not None and not _confirm_agent_external_action(
        "External disclosure: hosted chart analysis",
        disclosure,
    ):
        console.print("[yellow]Hosted chart disclosure declined.[/yellow]")
        raise typer.Exit(1)
    if reasoning_effort not in {"low", "medium", "high"}:
        console.print("[red]--reasoning-effort must be low, medium, or high.[/red]")
        raise typer.Exit(2)
    if isinstance(provider, OllamaProvider):
        selected_model = model or provider.model
        try:
            model_sizes = provider.installed_model_sizes()
            installed = frozenset(model_sizes)
            loaded = provider.loaded_models()
        except ProviderConfigurationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        if selected_model not in installed:
            console.print(
                f"[red]{selected_model} is not installed. "
                f"Run `trade models pull {selected_model}`.[/red]"
            )
            raise typer.Exit(1)
        assessment = _assess_ollama_model(
            settings,
            selected_model,
            model_sizes,
            loaded,
        )
        if assessment is not None:
            _render_model_assessment(assessment)
            if assessment.status == "block":
                console.print(
                    "[red]Chart analysis was not started. Close memory-heavy applications, "
                    "unload another Ollama model, or choose a smaller model.[/red]"
                )
                raise typer.Exit(1)
    try:
        result = analyze_chart(
            image_bytes,
            content_type,
            context,
            settings,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    upgrade_database()
    with SessionLocal() as db:
        scope = _current_scope(db)
        resolved_trade_plan_id = None
        if trade_plan:
            try:
                resolved_trade_plan_id = get_trade_plan(
                    db,
                    trade_plan,
                    scope=scope,
                ).id
            except TradeNotFoundError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
        evidence, run = record_chart_analysis(
            db,
            scope=scope,
            image_bytes=image_bytes,
            content_type=content_type,
            evidence_directory=settings.evidence_directory,
            analysis=result,
            provider=provider,
            model=model,
            policy_hash=_runtime_policy().content_hash,
            prompt=SYSTEM_PROMPT,
            source="cli:clipboard" if clipboard else "cli",
            market_time=observed_at,
            instrument=instrument,
            venue=venue,
            timeframe=timeframe,
            trade_plan_id=resolved_trade_plan_id,
        )
    _print_model(
        {
            "analysis": result,
            "provider": provider.name,
            "model": model or provider.model,
            "performance": getattr(provider, "last_performance", None),
            "evidence_id": evidence.id,
            "analysis_run_id": run.id,
        }
    )


@app.command("api")
def api_server(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload after source changes.")] = False,
    tls_certificate: Annotated[
        Path | None,
        typer.Option(
            "--tls-certificate",
            exists=True,
            dir_okay=False,
            help="PEM certificate for direct HTTPS.",
        ),
    ] = None,
    tls_private_key: Annotated[
        Path | None,
        typer.Option(
            "--tls-private-key",
            exists=True,
            dir_okay=False,
            help="PEM private key for direct HTTPS.",
        ),
    ] = None,
) -> None:
    """Run the optional HTTP and browser service."""
    api_key = secret_value(get_settings().trading_agent_api_key)
    if api_key is None or len(api_key) < 32:
        console.print(
            "[red]Set TRADING_AGENT_API_KEY to at least 32 random characters "
            "before starting the API.[/red]"
        )
        raise typer.Exit(1)
    if (tls_certificate is None) != (tls_private_key is None):
        console.print(
            "[red]Provide both --tls-certificate and --tls-private-key.[/red]"
        )
        raise typer.Exit(2)
    loopback = host.casefold() in {"127.0.0.1", "::1", "localhost"}
    if not loopback and tls_certificate is None:
        console.print(
            "[red]Non-loopback API binding requires direct TLS. Supply "
            "--tls-certificate and --tls-private-key, or bind to 127.0.0.1 "
            "behind an authenticated HTTPS reverse proxy.[/red]"
        )
        raise typer.Exit(2)
    certificate = tls_certificate.resolve() if tls_certificate is not None else None
    private_key = tls_private_key.resolve() if tls_private_key is not None else None
    if (
        (tls_certificate is not None and tls_certificate.is_symlink())
        or (tls_private_key is not None and tls_private_key.is_symlink())
    ):
        console.print("[red]TLS files cannot be symbolic links.[/red]")
        raise typer.Exit(2)
    if private_key is not None:
        key_metadata = private_key.stat()
        if (
            (hasattr(os, "getuid") and key_metadata.st_uid != os.getuid())
            or (os.name != "nt" and key_metadata.st_mode & 0o077)
        ):
            console.print(
                "[red]The TLS private key must be owned by the current user "
                "and have mode 600.[/red]"
            )
            raise typer.Exit(2)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        ssl_certfile=str(certificate) if certificate is not None else None,
        ssl_keyfile=str(private_key) if private_key is not None else None,
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
