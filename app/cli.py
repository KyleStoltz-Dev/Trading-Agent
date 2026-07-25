import asyncio
import json
import mimetypes
import uuid
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select

from app.config import Settings, get_settings, secret_value
from app.connectors import (
    BrokerConfigurationError,
    create_news_connector,
    create_oanda_connector,
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
from app.models import BrokerConnection, ConnectorCursor, ConversationSession
from app.policy import ExecutionHooks, PolicyEngine, ToolContext
from app.providers import ProviderConfigurationError, create_model_provider
from app.routing import AgentMode
from app.schemas import (
    BrokerPositionSizeRequest,
    InstrumentSpecificationCreate,
    ManagementEventCreate,
    PositionSizeRequest,
    ReflectionCreate,
    ReflectionRead,
    TradePlanCreate,
    TradePlanRead,
)
from app.services.agent import TradingAgent
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
from app.services.news import store_calendar_events, store_news_items
from app.services.risk import calculate_broker_position_size, calculate_position_size

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
console = Console()


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
    hooks = ExecutionHooks(
        _runtime_policy(),
        lambda action, values: assume_yes
        or _confirm_agent_mutation(action, values),
    )
    hooks.before_execute(
        ToolContext(
            name=name,
            arguments=arguments,
            mutating=mutating,
            deterministic=deterministic,
        )
    )


def _print_model(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    console.print_json(data=value)


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


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def _prompt_plan() -> TradePlanCreate:
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
        setup_name=typer.prompt("Setup name"),
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


def _confirm_agent_mutation(action: str, arguments: dict) -> bool:
    console.print(Panel(json.dumps(arguments, indent=2), title=action))
    return typer.confirm("Apply this journal change?")


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
    report = check_health(settings, engine, policy=policy)
    _render_health(report)
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
        current_mode: AgentMode = settings.agent_mode
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
            provider=provider,
            policy=policy,
        )
        console.print(
            Panel(
                f"Session {conversation.name} ({conversation.id})\n"
                f"Mode: {current_mode}\nType /help for commands; /exit to leave.",
                title="Trading Agent",
            )
        )
        while True:
            try:
                message = console.input("[bold cyan]you>[/bold cyan] ").strip()
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
                    "/mode auto|economy|balanced|deep · choose model effort\n"
                    "/develop <change> · hand a software change to the coding agent\n"
                    "Clear software-change requests also offer a development handoff.\n"
                    "Everything else is natural language; include a local chart path when needed."
                )
                continue
            if message == "/health":
                _render_health(check_health(settings, engine))
                continue
            if message.startswith("/mode"):
                requested_mode = message.removeprefix("/mode").strip()
                if requested_mode not in {"auto", "economy", "balanced", "deep"}:
                    console.print("[red]Use /mode auto|economy|balanced|deep[/red]")
                    continue
                current_mode = requested_mode  # type: ignore[assignment]
                console.print(f"[green]Model mode is now {current_mode}.[/green]")
                continue
            if detect_development_intent(message):
                try:
                    development = _run_development_handoff(settings, message)
                    add_turn(db, conversation, "user", message)
                    if development is None:
                        add_turn(
                            db,
                            conversation,
                            "assistant",
                            "Development handoff was offered and cancelled.",
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
                        )
                except Exception as exc:
                    console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
                continue

            history = conversation_history(db, conversation)
            try:
                reply = agent.respond(message, history, mode=current_mode)
            except Exception as exc:
                console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
                continue
            add_turn(db, conversation, "user", message)
            add_turn(db, conversation, "assistant", reply)
            route = agent.last_route
            route_label = (
                f"{route.mode} · {route.provider}/{route.model}" if route else "unknown route"
            )
            console.print(Panel(reply, title=f"Trading Agent · {route_label}"))


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
) -> None:
    """Check policy, model-provider configuration, and database connectivity."""
    _authorize_direct("get_system_health", {})
    report = check_health(get_settings(), engine, policy=_runtime_policy())
    _render_health(report)
    if strict and not report.ready:
        raise typer.Exit(1)


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
) -> None:
    """List recent journaled plans."""
    _authorize_direct("list_trade_plans", {"limit": limit})
    upgrade_database()
    with SessionLocal() as db:
        trades = list_trade_plans(db, limit=limit)
        serialized = [
            TradePlanRead.model_validate(trade).model_dump(mode="json") for trade in trades
        ]
        _print_model(serialized)


@journal_app.command("show")
def journal_show(trade_id: uuid.UUID) -> None:
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


@sessions_app.command("list")
def sessions_list(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """List recent interactive sessions."""
    upgrade_database()
    with SessionLocal() as db:
        conversations = list_conversations(db, limit)
        table = Table(title="Trading Agent sessions")
        table.add_column("Session ID")
        table.add_column("Name")
        table.add_column("Title")
        table.add_column("Updated")
        for conversation in conversations:
            table.add_row(
                str(conversation.id),
                conversation.name,
                conversation.title,
                str(conversation.updated_at),
            )
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
        for turn in conversation_history(db, conversation, limit=100):
            console.print(Panel(turn["content"], title=turn["role"]))


@app.command()
def review(
    trade_id: uuid.UUID,
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
    trade_plan_id: Annotated[uuid.UUID | None, typer.Option()] = None,
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
            "trade_plan_id": str(trade_plan_id) if trade_plan_id else None,
        },
        mutating=True,
    )
    content_type, _ = mimetypes.guess_type(image)
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        console.print("[red]Chart must be PNG, JPEG, or WebP.[/red]")
        raise typer.Exit(2)
    image_bytes = image.read_bytes()
    if len(image_bytes) > 10 * 1024 * 1024:
        console.print("[red]Image exceeds 10 MB.[/red]")
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
    settings = get_settings()
    provider = create_model_provider(settings)
    try:
        result = analyze_chart(
            image_bytes,
            content_type,
            context,
            settings,
            provider=provider,
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    upgrade_database()
    with SessionLocal() as db:
        evidence, run = record_chart_analysis(
            db,
            image_bytes=image_bytes,
            content_type=content_type,
            evidence_directory=settings.evidence_directory,
            analysis=result,
            provider=provider,
            policy_hash=_runtime_policy().content_hash,
            prompt=SYSTEM_PROMPT,
            source="cli",
            market_time=observed_at,
            instrument=instrument,
            venue=venue,
            timeframe=timeframe,
            trade_plan_id=trade_plan_id,
        )
    _print_model(
        {
            "analysis": result,
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
