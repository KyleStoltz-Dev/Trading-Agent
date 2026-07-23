import json
import mimetypes
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.models import ConversationSession
from app.schemas import (
    PositionSizeRequest,
    ReflectionCreate,
    ReflectionRead,
    TradePlanCreate,
    TradePlanRead,
)
from app.services.agent import TradingAgent
from app.services.chart_analysis import analyze_chart
from app.services.conversations import (
    add_turn,
    conversation_history,
    create_conversation,
    get_conversation,
    latest_conversation,
    list_conversations,
)
from app.services.health import HealthReport, check_health
from app.services.journal import (
    ReflectionExistsError,
    TradeNotFoundError,
    create_reflection,
    create_trade_plan,
    get_trade_plan,
    list_trade_plans,
)
from app.services.risk import calculate_position_size

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
console = Console()


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
    return TradePlanCreate(
        instrument=typer.prompt("Instrument", default="XAUUSD"),
        venue=typer.prompt("Venue", default="OANDA"),
        direction=typer.prompt("Direction (long/short)"),
        setup_name=typer.prompt("Setup name"),
        regime=typer.prompt("Regime", default="unknown"),
        context_timeframe=typer.prompt("Context timeframe", default="4h"),
        trigger_timeframe=typer.prompt("Trigger timeframe", default="5m"),
        entry=Decimal(typer.prompt("Entry")),
        stop=Decimal(typer.prompt("Stop")),
        target=Decimal(typer.prompt("Target")),
        account_equity=Decimal(typer.prompt("Account equity")),
        risk_percent=Decimal(typer.prompt("Risk percent", default="1")),
        value_per_price_unit=Decimal(typer.prompt("Value per price unit")),
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
    sizing = calculate_position_size(request)
    console.print(
        Panel(
            f"Risk: ${sizing.risk_amount}\n"
            f"Quantity: {sizing.quantity}\n"
            f"Planned R: {sizing.planned_r}",
            title="Plan preview",
        )
    )
    if not assume_yes and not typer.confirm("Save this trade plan?"):
        raise typer.Abort()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        trade = create_trade_plan(db, request)
        _print_model(TradePlanRead.model_validate(trade))


def _confirm_agent_mutation(action: str, arguments: dict) -> bool:
    console.print(Panel(json.dumps(arguments, indent=2), title=action))
    return typer.confirm("Apply this journal change?")


def _run_chat(session_id: uuid.UUID | None, new_session: bool) -> None:
    settings = get_settings()
    report = check_health(settings, engine)
    _render_health(report)
    if not report.ready:
        console.print(
            "[red]Interactive chat requires PostgreSQL. Start it locally or configure Neon, "
            "then run `trading-agent health`.[/red]"
        )
        raise typer.Exit(1)
    if not settings.openai_api_key:
        console.print("[red]Set OPENAI_API_KEY to use interactive chat.[/red]")
        raise typer.Exit(1)

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        conversation = get_conversation(db, session_id) if session_id else None
        if session_id and conversation is None:
            console.print(f"[red]Conversation {session_id} was not found.[/red]")
            raise typer.Exit(1)
        if conversation is None:
            conversation = (
                create_conversation(db)
                if new_session
                else latest_conversation(db) or create_conversation(db)
            )

        agent = TradingAgent(
            settings=settings,
            db=db,
            engine=engine,
            confirm_mutation=_confirm_agent_mutation,
        )
        console.print(
            Panel(
                f"Session {conversation.id}\nType /help for commands; /exit to leave.",
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
                    "Everything else is natural language; include a local chart path when needed."
                )
                continue
            if message == "/health":
                _render_health(check_health(settings, engine))
                continue

            history = conversation_history(db, conversation)
            try:
                reply = agent.respond(message, history)
            except Exception as exc:
                console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
                continue
            add_turn(db, conversation, "user", message)
            add_turn(db, conversation, "assistant", reply)
            console.print(Panel(reply, title="Trading Agent"))


@app.callback()
def main(
    ctx: typer.Context,
    session: Annotated[
        uuid.UUID | None,
        typer.Option("--session", help="Resume a saved interactive session UUID."),
    ] = None,
    new: Annotated[
        bool,
        typer.Option("--new", help="Start a new session instead of resuming the latest."),
    ] = False,
) -> None:
    """Start the interactive agent when no individual command is supplied."""
    if ctx.invoked_subcommand is None:
        _run_chat(session, new)


@app.command()
def chat(
    session: Annotated[
        uuid.UUID | None,
        typer.Option("--session", help="Resume a saved interactive session UUID."),
    ] = None,
    new: Annotated[
        bool,
        typer.Option("--new", help="Start a new session instead of resuming the latest."),
    ] = False,
) -> None:
    """Open the interactive agent explicitly."""
    _run_chat(session, new)


@app.command()
def health(
    strict: Annotated[
        bool,
        typer.Option(help="Exit unsuccessfully when any required check fails."),
    ] = False,
) -> None:
    """Check configuration, OpenAI credentials, and database connectivity."""
    report = check_health(get_settings(), engine)
    _render_health(report)
    if strict and not report.ready:
        raise typer.Exit(1)


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
    _print_model(calculate_position_size(request))


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
    except (ValidationError, OSError) as exc:
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
    except (ValidationError, OSError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@journal_app.command("list")
def journal_list(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """List recent journaled plans."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        trades = list_trade_plans(db, limit=limit)
        serialized = [
            TradePlanRead.model_validate(trade).model_dump(mode="json") for trade in trades
        ]
        _print_model(serialized)


@journal_app.command("show")
def journal_show(trade_id: uuid.UUID) -> None:
    """Show one journaled plan."""
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
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        conversations = list_conversations(db, limit)
        table = Table(title="Trading Agent sessions")
        table.add_column("Session ID")
        table.add_column("Title")
        table.add_column("Updated")
        for conversation in conversations:
            table.add_row(
                str(conversation.id),
                conversation.title,
                str(conversation.updated_at),
            )
        console.print(table)


@sessions_app.command("show")
def sessions_show(session_id: uuid.UUID) -> None:
    """Show the saved transcript for one session."""
    with SessionLocal() as db:
        conversation: ConversationSession | None = get_conversation(db, session_id)
        if conversation is None:
            console.print(f"[red]Conversation {session_id} was not found.[/red]")
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
    if not yes and not typer.confirm("Save this reflection?"):
        raise typer.Abort()
    with SessionLocal() as db:
        try:
            reflection = create_reflection(db, trade_id, request)
        except (TradeNotFoundError, ReflectionExistsError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        _print_model(ReflectionRead.model_validate(reflection))


@app.command()
def chart(
    image: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    context: Annotated[
        str,
        typer.Option(help="Known context; never inferred from the image."),
    ] = "",
) -> None:
    """Analyze a local chart screenshot."""
    content_type, _ = mimetypes.guess_type(image)
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        console.print("[red]Chart must be PNG, JPEG, or WebP.[/red]")
        raise typer.Exit(2)
    image_bytes = image.read_bytes()
    if len(image_bytes) > 10 * 1024 * 1024:
        console.print("[red]Image exceeds 10 MB.[/red]")
        raise typer.Exit(2)
    try:
        result = analyze_chart(image_bytes, content_type, context, get_settings())
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    _print_model(result)


@app.command("api")
def api_server(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload after source changes.")] = False,
) -> None:
    """Run the optional HTTP and browser service."""
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
