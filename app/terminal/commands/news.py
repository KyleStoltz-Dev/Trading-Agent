"""News command implementations; registration stays in app.cli."""

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

import typer
from rich.table import Table
from sqlalchemy import select

from app.connectors import (
    BrokerConfigurationError,
    create_news_connector,
    news_provider_configured,
)
from app.models import (
    EconomicEvent,
)
from app.services.event_glossary import event_insight
from app.services.news import (
    economic_event_history,
    store_calendar_events,
    store_news_items,
)
from app.terminal.runtime import CommandRuntime


def news_sync(
    runtime: CommandRuntime,
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
        connector = create_news_connector(runtime.get_settings())
    except (ValueError, BrokerConfigurationError) as exc:
        runtime.console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    country_values = [value.strip() for value in countries.split(",") if value.strip()]
    runtime.authorize(
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
    runtime.upgrade_database()

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
        runtime.console.print("[bold red]News sync unavailable[/bold red]")
        runtime.console.print(str(exc))
        runtime.console.print(
            "[dim]Previously stored calendar data remains available. "
            "Wait for the provider's retry window, then run this command again.[/dim]"
        )
        raise typer.Exit(1) from None
    with runtime.session_factory() as db:
        calendar_count = store_calendar_events(db, tuple(calendar))
        news_count = store_news_items(db, tuple(headlines))
    provider_name = runtime.get_settings().news_provider.replace("-", " ").title()
    runtime.console.print("[bold green]✓ News sync complete[/bold green]")
    runtime.console.print(f"[bold]Source[/bold]  {provider_name}")
    runtime.console.print(f"[bold]Calendar[/bold]  {len(calendar)} received · {calendar_count} new")
    if headlines:
        runtime.console.print(
            f"[bold]Headlines[/bold] {len(headlines)} received · {news_count} new"
        )
    elif runtime.get_settings().news_provider == "forex-factory":
        runtime.console.print(
            "[dim]Forex Factory supplies calendar events, not a headline API.[/dim]"
        )
    if not calendar and not headlines:
        runtime.console.print(
            "[yellow]No matching items were found for this date, currency, "
            "and impact filter.[/yellow]"
        )


def news_upcoming(
    runtime: CommandRuntime,
    hours: Annotated[int, typer.Option(min=1, max=168)] = 24,
    currencies: Annotated[str, typer.Option()] = "USD",
    minimum_importance: Annotated[int, typer.Option(min=0, max=3)] = 2,
    details: Annotated[bool, typer.Option("--details")] = False,
) -> None:
    """Show concise upcoming events from the stored calendar."""
    currency_values = tuple(
        dict.fromkeys(value.strip().upper() for value in currencies.split(",") if value.strip())
    )
    now = datetime.now(UTC)
    through = now + timedelta(hours=hours)
    runtime.upgrade_database()
    with runtime.session_factory() as db:
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
            statement = statement.where(EconomicEvent.currency.in_(currency_values))
        events = tuple(db.scalars(statement))

    runtime.console.print("[bold]Trading Agent: Upcoming economic events[/bold]")
    if not events:
        runtime.console.print("[yellow]No stored events match this window and filter.[/yellow]")
        runtime.console.print(
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
    runtime.console.print(table)
    runtime.console.print(
        f"[dim]{len(events)} event(s) through "
        f"{through.astimezone(local_timezone).strftime('%a %H:%M %Z')} · "
        "stored provider evidence, not trading instructions[/dim]"
    )
    if details:
        for event in events:
            insight = event_insight(event.title, event.currency)
            local_time = event.scheduled_at.astimezone(local_timezone)
            runtime.console.print()
            runtime.console.rule(f"[bold]{event.title}[/bold]", style="dim")
            runtime.console.print(
                f"[dim]{local_time.strftime('%A, %H:%M %Z')} · "
                f"{event.currency or '—'} · "
                f"{impact_names[event.importance]} impact[/dim]"
            )
            runtime.console.print()
            values = Table(show_header=True, box=None, pad_edge=False)
            values.add_column("Actual", min_width=12)
            values.add_column("Forecast", min_width=12)
            values.add_column("Previous", min_width=12)
            values.add_row(
                f"[bold]{event.actual or 'Pending'}[/bold]",
                event.forecast or "—",
                event.previous or "—",
            )
            runtime.console.print(values)
            runtime.console.print()
            runtime.console.print("[bold]What it measures[/bold]")
            runtime.console.print(insight.measures)
            runtime.console.print()
            runtime.console.print("[bold]Why markets watch it[/bold]")
            runtime.console.print(insight.why_markets_watch)
            runtime.console.print()
            if insight.sensitive_markets:
                runtime.console.print("[bold]Commonly sensitive markets[/bold]")
                runtime.console.print(" · ".join(insight.sensitive_markets))
                runtime.console.print()
            runtime.console.print("[bold yellow]Interpret carefully[/bold yellow]")
            runtime.console.print(f"[dim]{insight.interpretation_caution}[/dim]")
            if insight.source_label and insight.source_url:
                runtime.console.print()
                runtime.console.print("[bold]Primary reference[/bold]")
                runtime.console.print(insight.source_label)
                runtime.console.print(f"[link={insight.source_url}]{insight.source_url}[/link]")


def news_history(
    runtime: CommandRuntime,
    event: Annotated[str, typer.Argument(help="Event name, such as Core PCE or GDP.")],
    currency: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=50)] = 10,
) -> None:
    """Show stored past observations for one requested economic event."""
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            events = economic_event_history(
                db,
                event,
                currency=currency,
                limit=limit,
            )
        except ValueError as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

    runtime.console.print(f"[bold]Trading Agent: Previous {event.strip()} releases[/bold]")
    if not events:
        runtime.console.print("[yellow]No matching past releases are stored yet.[/yellow]")
        runtime.console.print(
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
    runtime.console.print(table)
    runtime.console.print(
        f"[dim]{len(events)} stored release(s) · "
        "values are provider evidence, not a directional signal[/dim]"
    )


def news_watch(
    runtime: CommandRuntime,
    interval_seconds: Annotated[int, typer.Option(min=30, max=3600)] = 300,
    alert_minutes: Annotated[int, typer.Option(min=1, max=1440)] = 60,
    currencies: Annotated[str, typer.Option()] = "USD",
    minimum_importance: Annotated[int, typer.Option(min=0, max=3)] = 2,
    once: Annotated[bool, typer.Option("--once")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Refresh the calendar on a schedule and print newly due event alerts."""
    settings = runtime.get_settings()
    if not news_provider_configured(settings):
        runtime.console.print(
            "[red]Select a configured news provider before starting calendar watch.[/red]"
        )
        raise typer.Exit(2)
    currency_values = tuple(
        dict.fromkeys(value.strip().upper() for value in currencies.split(",") if value.strip())
    )
    runtime.authorize(
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
    runtime.upgrade_database()
    runtime.console.print("[bold]Trading Agent: Economic calendar watch[/bold]")
    runtime.console.print(
        f"Refreshing every {interval_seconds}s · alert window {alert_minutes}m · "
        f"currencies {', '.join(currency_values) or 'all'}"
    )
    runtime.console.print("[dim]Press Ctrl-C to stop. No orders can be placed.[/dim]")
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
                runtime.console.print(
                    f"[yellow]Calendar refresh unavailable: {exc}. Using stored events.[/yellow]"
                )
            else:
                with runtime.session_factory() as db:
                    added = store_calendar_events(db, fetched)
                runtime.console.print(
                    f"[dim]{datetime.now().astimezone().strftime('%H:%M:%S %Z')} · "
                    f"{len(fetched)} received · {added} new[/dim]"
                )

            now = datetime.now(UTC)
            through = now + timedelta(minutes=alert_minutes)
            with runtime.session_factory() as db:
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
                    statement = statement.where(EconomicEvent.currency.in_(currency_values))
                due = tuple(db.scalars(statement))
            new_due = tuple(
                event for event in due if (event.source, event.source_event_id) not in notified
            )
            for event in new_due:
                local_time = event.scheduled_at.astimezone()
                runtime.console.print()
                runtime.console.print(
                    f"[bold yellow]Economic event approaching · "
                    f"{event.currency or '—'} · "
                    f"{local_time.strftime('%H:%M %Z')}[/bold yellow]"
                )
                runtime.console.print(event.title)
                runtime.console.print(
                    f"[dim]Impact {event.importance}/3 · source {event.source} · "
                    "untrusted calendar evidence[/dim]"
                )
                notified.add((event.source, event.source_event_id))
            if once:
                return
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        runtime.console.print("\n[dim]Calendar watch stopped.[/dim]")
