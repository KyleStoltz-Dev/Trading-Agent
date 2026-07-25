import asyncio
import json
import mimetypes
import shutil
import sys
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlalchemy import select

from app.config import Settings, default_config_path, get_settings, secret_value
from app.connectors import (
    BrokerConfigurationError,
    create_news_connector,
    create_oanda_connector,
)
from app.costs import (
    TokenUsage,
    calculate_cost,
    estimated_request_tokens,
    format_pricing,
    format_usd,
    model_pricing,
    output_budget_for_mode,
)
from app.db import (
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
    BrokerConnection,
    ConnectorCursor,
    ConversationSession,
)
from app.policy import ExecutionHooks, PolicyEngine, ToolContext
from app.providers import ProviderConfigurationError, create_model_provider
from app.providers.ollama_provider import OllamaProvider
from app.routing import AgentMode
from app.schemas import (
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
from app.services.agent import (
    TOOLS,
    PreparedAgentRequest,
    TradingAgent,
    UsedReference,
    _chart_destination,
    _read_approved_chart,
)
from app.services.analytics import build_edge_report
from app.services.broker_sync import synchronize_broker
from app.services.catalog import (
    active_instrument_specification,
    configure_account,
    configure_instrument_specification,
    create_playbook_version,
)
from app.services.chart_analysis import SYSTEM_PROMPT, analyze_chart
from app.services.conversations import (
    add_turn,
    conversation_history,
    conversation_transcript,
    create_conversation,
    latest_conversation,
    list_conversations,
    resolve_conversation,
)
from app.services.development import (
    DevelopmentService,
    DevelopmentSession,
    detect_development_intent,
    development_request,
)
from app.services.evidence import record_chart_analysis
from app.services.execution_ledger import record_management_event
from app.services.health import HealthReport, check_health
from app.services.journal import (
    ReflectionExistsError,
    TradeNotFoundError,
    create_reflection,
    create_trade_plan,
    get_trade_plan,
    list_trade_plans,
)
from app.services.knowledge_import import import_knowledge_path, import_knowledge_text
from app.services.market_features import (
    experiment_feature_correlations,
    measure_candle_features,
    strategy_experiment_report,
)
from app.services.mindset import create_mindset_check_in, list_mindset_check_ins
from app.services.news import store_calendar_events, store_news_items
from app.services.pretrade import (
    PreflightAssessment,
    assess_preflight,
    detect_preflight_intent,
    instrument_event_currencies,
    news_readiness,
    persist_preflight_workflow,
    pretrade_alerts,
    refresh_startup_calendar,
    render_pretrade_context,
    strategy_rules,
)
from app.services.risk import calculate_broker_position_size, calculate_position_size
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
from app.setup import (
    dependency_guidance,
    ensure_local_services,
    install_user_launcher,
    launcher_target_for_interpreter,
    ollama_profile_settings,
    provider_settings,
    pull_ollama_model,
    shell_path_hint,
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
mindset_app = typer.Typer(
    help="Record process readiness and predefined-risk acceptance."
)
app.add_typer(mindset_app, name="mindset")
console = Console()

STARTER_PROMPTS = (
    "Review this chart: /absolute/path/to/chart.png",
    "Help me build an XAUUSD New York premarket plan.",
    "Size this trade: equity 10000, risk 0.5%, entry 2350, stop 2345, target 2365.",
    "Review my recent trades and identify patterns worth testing as an edge.",
)


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
        lambda action, values: assume_yes
        or _confirm_agent_mutation(action, values),
    )
    hooks.before_execute(context)


def _print_model(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    console.print_json(data=jsonable_encoder(value))


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
        console.print(
            f"[dim]Optional setup: {', '.join(optional)} · use /health for details[/dim]"
        )


def _render_starter_prompts() -> None:
    console.print("[bold]Try asking:[/bold]")
    for prompt in STARTER_PROMPTS:
        console.print(f"  [cyan]›[/cyan] {prompt}")


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
            f"[{color}]{option.status}[/{color}]",
            option.capability,
        )
    console.print(table)
    console.print(
        "[dim]Only ready integrations are selectable. Planned entries document the "
        "next adapter boundary; they are not silently enabled.[/dim]"
    )


def _prompt_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _run_onboarding(db, settings: Settings) -> None:
    existing = get_trader_profile(db)
    console.print("[bold green]Trader onboarding[/bold green]")
    console.print(
        "This stores your profile in PostgreSQL. Credentials remain in the private "
        "configuration file and are never stored in the profile."
    )
    display_name = typer.prompt(
        "Display name",
        default=existing.display_name if existing else "Trader",
    )
    timezone = typer.prompt(
        "Timezone",
        default=existing.timezone if existing else "America/New_York",
    )
    experience = typer.prompt(
        "Experience level",
        default=existing.experience_level if existing and existing.experience_level else "advanced",
    )
    markets = _prompt_values(
        typer.prompt(
            "Markets/instruments (comma-separated)",
            default=(
                ",".join(existing.markets)
                if existing and existing.markets
                else "XAUUSD"
            ),
        )
    )
    sessions = _prompt_values(
        typer.prompt(
            "Trading sessions (comma-separated)",
            default=(
                ",".join(existing.sessions)
                if existing and existing.sessions
                else "New York"
            ),
        )
    )
    trading_style = typer.prompt(
        "Describe your trading style",
        default=(
            existing.trading_style
            if existing and existing.trading_style
            else "Discretionary multi-timeframe price-action trader."
        ),
    )
    goals = _prompt_values(
        typer.prompt(
            "Goals (comma-separated)",
            default=(
                ",".join(existing.goals)
                if existing and existing.goals
                else "consistency,measurable edge,process discipline"
            ),
        )
    )
    maximum_risk = typer.prompt(
        "Maximum planned risk percent",
        default=str(settings.maximum_trade_risk_percent),
    )
    profile = upsert_trader_profile(
        db,
        TraderProfileUpsert(
            display_name=display_name,
            timezone=timezone,
            experience_level=experience,
            trading_style=trading_style,
            markets=markets,
            sessions=sessions,
            goals=goals,
            risk_preferences={"maximum_trade_risk_percent": maximum_risk},
        ),
    )
    _render_integrations()
    broker = typer.prompt(
        "Ready broker (none/oanda)",
        default=settings.broker_provider,
    ).lower()
    news = typer.prompt(
        "Ready FX news/calendar provider (none/trading-economics)",
        default=settings.news_provider,
    ).lower()
    if broker not in {"none", "oanda"}:
        raise ValueError("only none or the ready OANDA connector can be selected")
    if news not in {"none", "trading-economics"}:
        raise ValueError(
            "only none or the ready Trading Economics connector can be selected"
        )
    update_env_file(
        default_config_path(),
        {
            "BROKER_PROVIDER": broker,
            "NEWS_PROVIDER": news,
        },
    )
    console.print(f"[green]Saved profile {profile.display_name} in PostgreSQL.[/green]")
    if broker == "oanda":
        console.print(
            f"Add OANDA_API_TOKEN and OANDA_ACCOUNT_ID to {default_config_path()}, "
            "keep OANDA_ENVIRONMENT=practice, then run `trade broker configure-oanda`."
        )
    if news == "trading-economics":
        console.print(
            f"Add TRADING_ECONOMICS_API_KEY to {default_config_path()}; startup will "
            "refresh the upcoming calendar and warn before trade-intent requests."
        )
    console.print(
        "Next: create an isolated strategy with `trade strategy create --help`, then "
        "import TXT/Markdown/JSON/CSV, a Discord ZIP, or a directory with "
        "`trade knowledge import --help`."
    )


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
    swap = (
        f"{snapshot.swap_percent:.1f}%"
        if snapshot.swap_percent is not None
        else "unknown"
    )
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


def _render_model_assessment(assessment: ModelFitAssessment) -> None:
    color = {"ok": "green", "warning": "yellow", "block": "red"}[assessment.status]
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
            (
                f"{model_sizes[model] / GIB:.1f} GiB"
                if model_sizes.get(model, 0)
                else "unknown"
            ),
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
    extras = sorted(installed - {
        settings.ollama_model,
        settings.ollama_economy_model or settings.ollama_model,
        settings.ollama_balanced_model or settings.ollama_model,
        settings.ollama_deep_model or settings.ollama_model,
    })
    if extras:
        console.print(f"[dim]Other installed models: {', '.join(extras)}[/dim]")


def _render_request_steps(
    prepared: PreparedAgentRequest,
    provider_name: str,
    context_count: int,
) -> None:
    route = prepared.route
    console.print(
        f"[dim]Context  ✓ {context_count} "
        f"{'resource' if context_count == 1 else 'resources'} selected[/dim]"
    )
    console.print(
        f"[dim]Route    ✓ {route.mode} · {provider_name}/{route.model} "
        f"· {route.reasoning_effort} reasoning[/dim]"
    )
    pricing = model_pricing(provider_name, route.model)
    if pricing is None:
        console.print("[yellow]Cost     ? pricing unavailable for this model[/yellow]")
        return
    input_tokens = estimated_request_tokens(
        instructions=prepared.instructions,
        message=prepared.message,
        history=prepared.history,
        tools=TOOLS,
    )
    output_tokens = output_budget_for_mode(route.mode)
    estimate = calculate_cost(
        pricing,
        TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    if provider_name == "ollama":
        console.print("[dim]Cost     ✓ $0 API cost · local inference[/dim]")
    else:
        console.print(
            f"[dim]Cost     ~ {format_usd(estimate)} first-response estimate "
            f"· ~{input_tokens:,} input + up to {output_tokens:,} output tokens[/dim]"
        )


def _render_agent_reply(
    reply: str,
    route_label: str,
    context_count: int,
    provider_name: str,
    model: str,
    usage: TokenUsage,
    references: list[UsedReference],
    performance: dict[str, float] | None = None,
) -> None:
    console.print()
    console.print("[bold green]Trading Agent[/bold green]")
    console.print(Markdown(reply))
    usage_label = ""
    pricing = model_pricing(provider_name, model)
    if provider_name == "ollama":
        usage_label = (
            f" · $0 API · {usage.input_tokens:,} in/{usage.output_tokens:,} out"
            if usage.input_tokens or usage.output_tokens
            else " · $0 API"
        )
    elif pricing and (usage.input_tokens or usage.output_tokens):
        cost = calculate_cost(pricing, usage)
        usage_label = (
            f" · {format_usd(cost)} est. API"
            f" · {usage.input_tokens:,} in/{usage.output_tokens:,} out"
        )
    console.print(
        f"[dim]{route_label} · {context_count} context "
        f"{'resource' if context_count == 1 else 'resources'}"
        f"{usage_label} · /context to inspect[/dim]"
    )
    if performance:
        console.print(
            "[dim]Local performance · "
            f"{performance.get('output_tokens_per_second', 0):g} output tok/s · "
            f"{performance.get('load_seconds', 0):g}s load · "
            f"{performance.get('total_seconds', 0):g}s total[/dim]"
        )
    console.print("[bold]References used[/bold]")
    for reference in references:
        timestamp = f" · {reference.retrieved_at}" if reference.retrieved_at else ""
        console.print(
            Text(
                f"  • {reference.kind}: {reference.label} — "
                f"{reference.locator}{timestamp}",
                style="dim",
            )
        )
    console.print()


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def _prompt_plan(setup_name: str | None = None) -> TradePlanCreate:
    market_time = typer.prompt(
        "Market time (ISO-8601 with timezone; blank if unknown)",
        default="",
    )
    sizing_provider = typer.prompt(
        "Sizing provider (blank for manual value-per-price-unit)",
        default="",
    )
    sizing_symbol = (
        typer.prompt("Broker symbol", default="XAU_USD")
        if sizing_provider
        else None
    )
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
            Decimal("1")
            if sizing_provider
            else Decimal(typer.prompt("Value per price unit"))
        ),
        sizing_provider=sizing_provider or None,
        sizing_symbol=sizing_symbol,
        available_margin=(
            Decimal(typer.prompt("Available margin"))
            if sizing_provider
            else None
        ),
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
            risk_display = (
                sizing.estimated_loss_at_stop + sizing.estimated_costs
            )
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
        trade = create_trade_plan(
            db,
            request,
            policy_hash=_runtime_policy().content_hash,
            source="cli",
            maximum_risk_percent=maximum_risk,
        )
        _print_model(TradePlanRead.model_validate(trade))


def _prompt_rule_answer(rule_kind: str, text: str) -> bool | None:
    question = (
        "Requirement met"
        if rule_kind == "requirement"
        else "Exclusion applies"
    )
    while True:
        answer = typer.prompt(
            f"{question}? {text} (yes/no/unknown)",
        ).strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        if answer in {"u", "unknown", "unclear"}:
            return None
        console.print("[yellow]Enter yes, no, or unknown.[/yellow]")


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
        *(f"{assessment.component_scores[key]}%" for key in (
            "strategy", "risk", "mindset", "evidence", "news"
        ))
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
    console.print(
        f"News state: {assessment.news.status} · {assessment.news.detail}"
    )
    if assessment.alerts:
        console.print(render_pretrade_context(list(assessment.alerts)))


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
            conversation = (
                resolve_conversation(db, session)
                if session
                else latest_conversation(db)
            )
            if conversation is None:
                raise LookupError(
                    "no session exists; run `trade` and select a strategy first"
                )
            active = active_session_strategy(db, conversation)
            if active is None:
                raise LookupError(
                    "no exact strategy is active; use `/strategy use NAME` in `trade`"
                )
            playbook, version = active
            definition = version.definition
            setups = definition.get("setups")
            setup_names = [
                str(item["key"])
                for item in setups
                if isinstance(setups, list)
                and isinstance(item, dict)
                and isinstance(item.get("key"), str)
            ] if isinstance(setups, list) else []
            selected_setup = setup_key
            if selected_setup is None and len(setup_names) > 1:
                selected_setup = typer.prompt(
                    f"Setup key ({', '.join(setup_names)})"
                )
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

            if (
                settings.news_provider == "trading-economics"
                and settings.trading_economics_api_key
            ):
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
                    settings.news_provider == "trading-economics"
                    and bool(settings.trading_economics_api_key)
                ),
            )

            market_context: dict = {}
            if live_market:
                if settings.broker_provider != "oanda":
                    raise BrokerConfigurationError(
                        "--live-market currently requires BROKER_PROVIDER=oanda"
                    )
                connector = create_oanda_connector(settings)
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
                rule.rule_id: _prompt_rule_answer(rule.kind, rule.text)
                for rule in rules
            }
            readiness = int(typer.prompt("Readiness (1-5)", default="3"))
            accepted_risk = typer.confirm(
                "Do you fully accept the predefined loss if the stop is reached?",
                default=False,
            )
            emotions = _split_list(
                typer.prompt("Emotions (separate with |)", default="")
            )
            mindset_note = typer.prompt(
                "Mindset/process note",
                default="",
            ).strip() or None

            if request.sizing_provider and request.sizing_symbol:
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
                readiness=readiness,
                accepted_risk=accepted_risk,
                emotion_tags=emotions,
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
                "accepted_risk": accepted_risk,
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
                assessment=assessment,
                playbook_version_id=version.id,
                mindset_request=MindsetCheckInCreate(
                    phase="pre_trade",
                    readiness=readiness,
                    accepted_risk=accepted_risk,
                    emotion_tags=emotions,
                    note=mindset_note,
                ),
                decision=decision,
                policy_hash=_runtime_policy().content_hash,
                market_context=market_context,
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

    playbook_version_id = conversation.active_playbook_version_id
    add_turn(
        db,
        conversation,
        "user",
        message,
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
            playbook_version_id=playbook_version_id,
        )
        console.print("[dim]Preflight skipped. Returning to chat.[/dim]")
        return True

    try:
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
        playbook_version_id=playbook_version_id,
    )
    console.print("[dim]Preflight complete. Returning to chat.[/dim]")
    return True


def _confirm_agent_mutation(action: str, arguments: dict) -> bool:
    console.print(Panel(json.dumps(arguments, indent=2), title=action))
    return typer.confirm("Apply this exact database change?")


def _confirm_agent_external_action(action: str, arguments: dict) -> bool:
    console.print(Panel(json.dumps(arguments, indent=2), title=action))
    return typer.confirm("Send this exact query to the external search provider?")


def _render_development_session(session: object) -> None:
    validation = getattr(session, "validation", None) or []
    checks = "\n".join(
        f"{'✓' if item['passed'] else '✗'} {item['command']}" for item in validation
    )
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
    if (
        settings.development_approval_flow == "scope_only"
        and session.status == "needs_review"
    ):
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
    report = check_health(
        settings,
        engine,
        policy=policy,
        model_smoke_test=settings.startup_model_smoke_test,
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

    if settings.database_auto_migrate:
        upgrade_database()
    with SessionLocal() as db:
        if (
            settings.startup_news_sync
            and settings.news_provider == "trading-economics"
            and settings.trading_economics_api_key
        ):
            try:
                refreshed = asyncio.run(refresh_startup_calendar(settings, db))
            except Exception as exc:
                console.print(
                    f"[yellow]Startup calendar refresh failed: "
                    f"{type(exc).__name__}[/yellow]"
                )
            else:
                console.print(
                    f"[green]✓ Economic calendar refreshed · "
                    f"{refreshed} new events[/green]"
                )
        if get_trader_profile(db) is None:
            console.print(
                "[yellow]No trader profile exists. Guided onboarding connects your "
                "style, markets, integrations, and strategy imports to PostgreSQL.[/yellow]"
            )
            if typer.confirm("Run onboarding now?", default=True):
                _run_onboarding(db, settings)
                console.print(
                    "[yellow]Configuration selections were saved. Restart `trade` after "
                    "adding any broker/news keys.[/yellow]"
                )
        current_mode: AgentMode = settings.agent_mode
        current_model_override: str | None = None
        last_runtime_model: str | None = None
        conversation = (
            resolve_conversation(db, session_reference) if session_reference else None
        )
        if session_reference and conversation is None:
            console.print(f"[red]Conversation {session_reference} was not found.[/red]")
            raise typer.Exit(1)
        if conversation is None:
            conversation = (
                create_conversation(db, name=session_name)
                if new_session
                else latest_conversation(db) or create_conversation(db)
            )

        agent = TradingAgent(
            settings=settings,
            db=db,
            engine=engine,
            confirm_mutation=_confirm_agent_mutation,
            confirm_external_action=_confirm_agent_external_action,
            provider=provider,
            policy=policy,
            active_playbook_version_id=conversation.active_playbook_version_id,
        )
        active_strategy = active_session_strategy(db, conversation)
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
        _render_starter_prompts()
        console.print(
            "[dim]/help commands · /examples starter prompts · /cost model pricing "
            "· /model local model · /sources references · /exit leave[/dim]\n"
        )
        while True:
            try:
                message = console.input("[bold cyan]You[/bold cyan] [bold]❯[/bold] ").strip()
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
                    "/examples · show starter prompts\n"
                    "/cost · show configured model prices\n"
                    "/sources · show references used for the last response\n"
                    "/context · show harness resources selected for the last response\n"
                    "/strategy · show active isolated strategy\n"
                    "/strategy use NAME · switch to exactly one strategy version\n"
                    "/strategy clear · disable strategy-specific retrieval\n"
                    "/mode auto|economy|balanced|deep · choose model effort\n"
                    "/model · show local model profiles\n"
                    "/model use NAME · override the local model for this session\n"
                    "/model auto · return to automatic profile routing\n"
                    "/develop <change> · hand a software change to the coding agent\n"
                    "Clear software-change requests also offer a development handoff.\n"
                    "Everything else is natural language; include a local chart path when needed."
                )
                continue
            if message == "/examples":
                _render_starter_prompts()
                continue
            if message == "/cost":
                _render_cost_table(settings, provider.name, provider.model)
                continue
            if message == "/sources":
                if not agent.last_references:
                    console.print("No response references recorded yet.")
                else:
                    for reference in agent.last_references:
                        timestamp = (
                            f" · {reference.retrieved_at}"
                            if reference.retrieved_at
                            else ""
                        )
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
            if message == "/strategy":
                active_strategy = active_session_strategy(db, conversation)
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
                    active_strategy = set_session_strategy(
                        db,
                        conversation,
                        strategy_name,
                    )
                except LookupError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue
                agent.active_playbook_version_id = active_strategy[1].id
                console.print(
                    f"[green]Strategy isolation switched to "
                    f"{active_strategy[0].name} v{active_strategy[1].version} only.[/green]"
                )
                continue
            if message == "/strategy clear":
                set_session_strategy(db, conversation, None)
                agent.active_playbook_version_id = None
                active_strategy = None
                console.print(
                    "[green]Strategy context cleared; strategy knowledge will not be "
                    "retrieved.[/green]"
                )
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
                    console.print(
                        f"[green]Session override: {current_model_override}[/green]"
                    )
                else:
                    console.print("[dim]Session override: automatic routing[/dim]")
                continue
            if message == "/model auto":
                if isinstance(provider, OllamaProvider) and last_runtime_model:
                    try:
                        provider.unload_model(last_runtime_model)
                    except ProviderConfigurationError as exc:
                        console.print(f"[yellow]{exc}[/yellow]")
                    last_runtime_model = None
                current_model_override = None
                console.print("[green]Returned to automatic model-profile routing.[/green]")
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
                        provider.unload_model(last_runtime_model)
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
                        playbook_version_id=request_playbook_version_id,
                    )
                    if development is None:
                        add_turn(
                            db,
                            conversation,
                            "assistant",
                            "Development handoff was offered and cancelled.",
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
                            playbook_version_id=request_playbook_version_id,
                        )
                except Exception as exc:
                    console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
                continue

            if _handle_chat_preflight_intent(db, conversation, message):
                continue

            request_playbook_version_id = conversation.active_playbook_version_id
            history = conversation_history(
                db,
                conversation,
                playbook_version_id=request_playbook_version_id,
            )
            try:
                alerts = pretrade_alerts(
                    db,
                    message,
                    currencies=instrument_event_currencies(message),
                    window_minutes=settings.pretrade_news_window_minutes,
                    minimum_importance=settings.pretrade_minimum_event_importance,
                )
                alert_references = [
                    UsedReference(
                        kind="calendar",
                        label=alert.title,
                        locator=(
                            alert.source_url
                            or f"economic-event:{alert.event_id}"
                        ),
                        retrieved_at=alert.retrieved_at.isoformat(),
                    )
                    for alert in alerts
                ]
                if alerts:
                    console.print("[bold yellow]Upcoming event risk[/bold yellow]")
                    for alert in alerts:
                        console.print(
                            Text(
                                f"  {alert.minutes_from_now:+}m · importance "
                                f"{alert.importance} · {alert.country} · {alert.title}"
                            )
                        )
                prepared = agent.prepare(
                    message,
                    history,
                    current_mode,
                    evidence_context=render_pretrade_context(alerts),
                    evidence_references=alert_references,
                    model_override=current_model_override,
                )
                _render_request_steps(
                    prepared,
                    provider.name,
                    len(agent.last_harness_context.paths),
                )
                if (
                    isinstance(provider, OllamaProvider)
                    and last_runtime_model
                    and last_runtime_model != prepared.route.model
                ):
                    provider.unload_model(last_runtime_model)
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
                                "explicit local-model override is unsafe at current "
                                "system pressure"
                            )
                        fallback_model = (
                            settings.ollama_economy_model or settings.ollama_model
                        )
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
                                "no configured local model safely fits the current "
                                "system pressure"
                            )
                        _render_model_assessment(assessment)
                        console.print(
                            f"[yellow]Falling back to {fallback_model} for this request.[/yellow]"
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
                        _render_model_assessment(assessment)
                with console.status(
                    "[bold green]Thinking and checking tools…[/bold green]",
                    spinner="dots",
                ):
                    reply = agent.respond(
                        message,
                        history,
                        mode=current_mode,
                        prepared=prepared,
                    )
                if isinstance(provider, OllamaProvider):
                    last_runtime_model = prepared.route.model
            except Exception as exc:
                console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
                continue
            add_turn(
                db,
                conversation,
                "user",
                message,
                playbook_version_id=request_playbook_version_id,
            )
            add_turn(
                db,
                conversation,
                "assistant",
                reply,
                playbook_version_id=request_playbook_version_id,
            )
            route = agent.last_route
            route_label = (
                f"{route.mode} · {route.provider}/{route.model}" if route else "unknown route"
            )
            context_paths = agent.last_harness_context.paths
            usage = getattr(provider, "last_usage", TokenUsage())
            _render_agent_reply(
                reply,
                route_label,
                len(context_paths),
                route.provider if route else provider.name,
                route.model if route else provider.model,
                usage,
                agent.last_references,
                getattr(provider, "last_performance", None),
            )


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
def integrations_command() -> None:
    """List ready, adapter-only, and planned broker/news integrations."""
    _render_integrations()
    for option in integration_options():
        console.print(
            Text(
                f"{option.name}: {option.setup}\n  {option.documentation}",
                style="dim",
            )
        )


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
    console.print(
        f"[cyan]Downloading {model}; model files may be tens of gigabytes…[/cyan]"
    )
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
        typer.Option(
            help="default, economy, balanced, deep, quality (balanced+deep), or all."
        ),
    ] = "quality",
) -> None:
    """Persist an installed model for one or more routing profiles."""
    if tier not in {"default", "economy", "balanced", "deep", "quality", "all"}:
        console.print(
            "[red]Tier must be default, economy, balanced, deep, quality, or all.[/red]"
        )
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


@app.command("onboard")
def onboard_command() -> None:
    """Store the trader profile and choose ready broker/news integrations."""
    settings = get_settings()
    for message in ensure_local_services(settings, engine):
        console.print(f"[cyan]{message}[/cyan]")
    upgrade_database()
    with SessionLocal() as db:
        try:
            _run_onboarding(db, settings)
        except (ValueError, LookupError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


@app.command("setup")
def setup_agent(
    provider: Annotated[
        str | None,
        typer.Option(help="Model provider: ollama, openai, or anthropic."),
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
        typer.Option(help="Database mode: local, neon, or custom."),
    ] = None,
    broker: Annotated[
        str | None,
        typer.Option(help="Ready broker connector: none or oanda."),
    ] = None,
    news: Annotated[
        str | None,
        typer.Option(help="Ready news connector: none or trading-economics."),
    ] = None,
) -> None:
    """Configure the provider and install the short `trade` launcher."""
    selected = (provider or typer.prompt(
        "Provider (ollama/openai/anthropic)",
        default="ollama",
    )).lower()
    if selected not in {"ollama", "openai", "anthropic"}:
        console.print("[red]Provider must be ollama, openai, or anthropic.[/red]")
        raise typer.Exit(2)
    _render_integrations()
    selected_database = (
        database
        or (
            "local"
            if yes
            else typer.prompt("Database (local/neon/custom)", default="local")
        )
    ).lower()
    selected_broker = (
        broker
        or (
            "none"
            if yes
            else typer.prompt("Broker (none/oanda)", default="none")
        )
    ).lower()
    selected_news = (
        news
        or (
            "none"
            if yes
            else typer.prompt(
                "FX news/calendar (none/trading-economics)",
                default="none",
            )
        )
    ).lower()
    if selected_database not in {"local", "neon", "custom"}:
        console.print("[red]Database must be local, neon, or custom.[/red]")
        raise typer.Exit(2)
    if selected_broker not in {"none", "oanda"}:
        console.print("[red]Only none or the ready OANDA connector is selectable.[/red]")
        raise typer.Exit(2)
    if selected_news not in {"none", "trading-economics"}:
        console.print(
            "[red]Only none or the ready Trading Economics connector is selectable.[/red]"
        )
        raise typer.Exit(2)

    resolved_config = (config or default_config_path()).expanduser().resolve()
    try:
        values = provider_settings(selected, model)  # type: ignore[arg-type]
        values.update(
            {
                "DATABASE_MODE": selected_database,
                "BROKER_PROVIDER": selected_broker,
                "NEWS_PROVIDER": selected_news,
            }
        )
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
    if selected_news == "trading-economics":
        console.print(
            f"[yellow]Add TRADING_ECONOMICS_API_KEY to {resolved_config}.[/yellow]"
        )
    console.print(
        "[bold green]Environment setup complete. Reopen Terminal, run `trade onboard`, "
        "then run `trade`.[/bold green]"
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
        if (
            settings.development_approval_flow == "scope_only"
            and session.status == "needs_review"
        ):
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
        trades = list_trade_plans(db, limit=limit)
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
                str(trade.created_at),
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
        try:
            trade = get_trade_plan(db, trade_id)
        except TradeNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(TradePlanRead.model_validate(trade))


def _mindset_strategy_version_id(db, session: str | None) -> uuid.UUID:
    conversation = (
        resolve_conversation(db, session)
        if session
        else latest_conversation(db)
    )
    if conversation is None or conversation.active_playbook_version_id is None:
        raise LookupError(
            "mindset check-ins require an exact active strategy; start `trade` "
            "and use `/strategy use NAME` first"
        )
    if active_session_strategy(db, conversation) is None:
        raise LookupError("the session's active strategy version no longer exists")
    return conversation.active_playbook_version_id


@mindset_app.command("check")
def mindset_check(
    phase: Annotated[
        str,
        typer.Option(
            help="pre_session, pre_trade, during_trade, or post_trade."
        ),
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
            playbook_version_id = _mindset_strategy_version_id(db, session)
            _print_model(
                create_mindset_check_in(
                    db,
                    request,
                    playbook_version_id=playbook_version_id,
                )
            )
    except (ValidationError, ValueError, LookupError) as exc:
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
            playbook_version_id = _mindset_strategy_version_id(db, session)
            check_ins = list_mindset_check_ins(
                db,
                playbook_version_id=playbook_version_id,
                limit=limit,
                phase=phase.replace("-", "_") if phase else None,
            )
        table = Table(title="Mindset check-ins")
        table.add_column("Created")
        table.add_column("Phase")
        table.add_column("Ready")
        table.add_column("Risk accepted")
        table.add_column("Emotions")
        table.add_column("Trade")
        table.add_column("Process note")
        for item in check_ins:
            table.add_row(
                str(item.created_at),
                item.phase,
                f"{item.readiness}/5",
                "yes" if item.accepted_risk else "no",
                ", ".join(item.emotion_tags) or "-",
                item.trade_reference or "-",
                item.note or "-",
            )
        console.print(table)
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@sessions_app.command("list")
def sessions_list(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    show_internal_ids: Annotated[bool, typer.Option()] = False,
) -> None:
    """List recent interactive sessions."""
    upgrade_database()
    with SessionLocal() as db:
        conversations = list_conversations(db, limit)
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
    if not settings.oanda_account_id or not settings.oanda_api_token:
        console.print(
            "[red]Set OANDA_ACCOUNT_ID and OANDA_API_TOKEN in .env first.[/red]"
        )
        raise typer.Exit(1)
    arguments = {
        "provider": "oanda-v20",
        "label": label,
        "currency": currency,
        "environment": settings.oanda_environment,
    }
    _authorize_direct(
        "configure_broker_connection",
        arguments,
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    with SessionLocal() as db:
        account, connection = configure_account(
            db,
            broker="OANDA",
            external_account_id=secret_value(settings.oanda_account_id),
            label=label,
            currency=currency,
            mode=settings.oanda_environment,
            provider="oanda-v20",
            environment=settings.oanda_environment,
            config_reference="env:OANDA_API_TOKEN",
        )
        _print_model(
            {
                "account_id": account.id,
                "connection_id": connection.id,
                "provider": connection.provider,
                "environment": connection.environment,
            }
        )


def _configured_oanda_connection(db) -> BrokerConnection:
    settings = get_settings()
    statement = select(BrokerConnection).where(
        BrokerConnection.provider == "oanda-v20"
    )
    connections = list(db.scalars(statement))
    matches = [
        item
        for item in connections
        if item.account.external_account_id == secret_value(settings.oanda_account_id)
    ]
    if len(matches) != 1:
        raise LookupError(
            "run `trading-agent broker configure-oanda` for the configured account"
        )
    return matches[0]


@broker_app.command("quote")
def broker_quote(instrument: str) -> None:
    """Read one live OANDA quote with market and retrieval timestamps."""
    _authorize_direct("get_live_quote", {"instrument": instrument})
    try:
        connector = create_oanda_connector(get_settings())
    except BrokerConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    async def read():
        try:
            return await connector.latest_quote(instrument)
        finally:
            await connector.aclose()

    _print_model(asyncio.run(read()))


@broker_app.command("sync")
def broker_sync(
    from_transaction_id: Annotated[
        str | None,
        typer.Option(
            "--from-transaction-id",
            help="Explicit first OANDA transaction id for a one-time history import.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Import new OANDA transactions and reconcile account/position snapshots."""
    _authorize_direct(
        "synchronize_broker",
        {
            "provider": "oanda-v20",
            "from_transaction_id": from_transaction_id,
        },
        mutating=True,
        assume_yes=yes,
    )
    upgrade_database()
    try:
        connector = create_oanda_connector(get_settings())
    except BrokerConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    with SessionLocal() as db:
        try:
            connection = _configured_oanda_connection(db)
            if from_transaction_id is not None:
                if not from_transaction_id.isdigit():
                    raise ValueError("--from-transaction-id must contain only digits")
                existing_cursor = db.scalar(
                    select(ConnectorCursor).where(
                        ConnectorCursor.connection_id == connection.id,
                        ConnectorCursor.stream_name == "transactions",
                    )
                )
                if existing_cursor is not None:
                    raise ValueError(
                        "a transaction cursor already exists; refusing to rewind it"
                    )
                db.add(
                    ConnectorCursor(
                        connection_id=connection.id,
                        stream_name="transactions",
                        cursor_value=from_transaction_id,
                    )
                )
                db.flush()

            async def synchronize():
                try:
                    return await synchronize_broker(
                        db,
                        connection_id=connection.id,
                        connector=connector,
                    )
                finally:
                    await connector.aclose()

            _print_model(asyncio.run(synchronize()))
        except (LookupError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc


@edge_app.command("report")
def edge_report(
    minimum_sample: Annotated[int, typer.Option(min=5, max=1000)] = 30,
) -> None:
    """Report expectancy only by stable setup/instrument/regime/timeframe segments."""
    _authorize_direct("build_edge_report", {"minimum_sample": minimum_sample})
    upgrade_database()
    with SessionLocal() as db:
        _print_model(build_edge_report(db, minimum_sample))


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
        version = create_playbook_version(
            db,
            name=name,
            definition=definition,
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
        version = create_playbook_version(
            db,
            name=name,
            definition=definition,
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
        _print_model([item.model_dump(mode="json") for item in list_strategy_summaries(db)])


@strategy_app.command("use")
def strategy_use(
    name: Annotated[str, typer.Argument(help="Strategy name to isolate in the session.")],
    session: Annotated[str | None, typer.Option(help="Session name or UUID.")] = None,
) -> None:
    """Select exactly one strategy version for a conversation."""
    upgrade_database()
    with SessionLocal() as db:
        conversation = (
            resolve_conversation(db, session)
            if session
            else latest_conversation(db)
        )
        if conversation is None:
            console.print("[red]No conversation session exists yet.[/red]")
            raise typer.Exit(1)
        try:
            playbook, version = set_session_strategy(db, conversation, name)
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(
            f"[green]Session {conversation.name} now uses only "
            f"{playbook.name} v{version.version} ({version.content_hash[:12]}).[/green]"
        )


@strategy_app.command("clear")
def strategy_clear(
    session: Annotated[str | None, typer.Option(help="Session name or UUID.")] = None,
) -> None:
    """Clear strategy-specific retrieval for a conversation."""
    upgrade_database()
    with SessionLocal() as db:
        conversation = (
            resolve_conversation(db, session)
            if session
            else latest_conversation(db)
        )
        if conversation is None:
            console.print("[red]No conversation session exists yet.[/red]")
            raise typer.Exit(1)
        set_session_strategy(db, conversation, None)
        console.print(
            f"[green]Session {conversation.name} has no active strategy context.[/green]"
        )


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
            result = import_knowledge_path(db, path, strategy)
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
            _print_model(import_knowledge_text(db, text, strategy, name))
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
        try:
            playbook, version = resolve_strategy_version(db, strategy)
            items = search_strategy_knowledge(db, version.id, query, limit)
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
                    create_strategy_experiment(db, request)
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
            sample = add_strategy_test_sample(db, experiment_id, request)
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
            experiment = resolve_strategy_experiment(db, experiment_id)
        except LookupError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(
            experiment_feature_correlations(
                db,
                experiment.id,
                minimum_samples=minimum_samples,
            )
        )


@experiment_app.command("report")
def experiment_report(experiment_id: str) -> None:
    """Show sample counts, exclusions, expectancy, and feature correlations."""
    upgrade_database()
    with SessionLocal() as db:
        try:
            _print_model(strategy_experiment_report(db, experiment_id))
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
                    complete_strategy_experiment(db, experiment_id)
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
            experiment = resolve_strategy_experiment(db, experiment_id)
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

    calendar, headlines = asyncio.run(fetch())
    with SessionLocal() as db:
        calendar_count = store_calendar_events(db, tuple(calendar))
        news_count = store_news_items(db, tuple(headlines))
    _print_model(
        {
            "calendar_received": len(calendar),
            "calendar_added": calendar_count,
            "news_received": len(headlines),
            "news_added": news_count,
        }
    )


@sessions_app.command("show")
def sessions_show(session: str) -> None:
    """Show the saved transcript for one session."""
    upgrade_database()
    with SessionLocal() as db:
        conversation: ConversationSession | None = resolve_conversation(db, session)
        if conversation is None:
            console.print(f"[red]Conversation {session} was not found.[/red]")
            raise typer.Exit(1)
        for turn in conversation_transcript(db, conversation, limit=100):
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
            reflection = create_reflection(db, trade_id, request)
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
            event = record_management_event(db, trade_id, request)
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
    image: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
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
    """Analyze a local chart screenshot."""
    _authorize_direct(
        "analyze_chart",
        {
            "image_path": str(image),
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
    if destination is not None and not _confirm_agent_external_action(
        "External disclosure: hosted chart analysis",
        {
            "provider": provider.name,
            "destination": destination,
            "image_path": str(resolved_image),
            "content_type": content_type,
            "image_bytes": len(image_bytes),
            "context": context,
        },
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
        resolved_trade_plan_id = None
        if trade_plan:
            try:
                resolved_trade_plan_id = get_trade_plan(db, trade_plan).id
            except TradeNotFoundError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
        evidence, run = record_chart_analysis(
            db,
            image_bytes=image_bytes,
            content_type=content_type,
            evidence_directory=settings.evidence_directory,
            analysis=result,
            provider=provider,
            model=model,
            policy_hash=_runtime_policy().content_hash,
            prompt=SYSTEM_PROMPT,
            source="cli",
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
) -> None:
    """Run the optional HTTP and browser service."""
    api_key = secret_value(get_settings().trading_agent_api_key)
    if api_key is None or len(api_key) < 32:
        console.print(
            "[red]Set TRADING_AGENT_API_KEY to at least 32 random characters "
            "before starting the API.[/red]"
        )
        raise typer.Exit(1)
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
