from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from rich.console import Console

import app.cli as cli_module
from app.services.chat_calendar import parse_chat_calendar_request


def test_parse_today_calendar_defaults_to_every_currency_and_impact() -> None:
    request = parse_chat_calendar_request(
        "Show me today's economic news.",
        now=datetime(2026, 9, 2, 23, 45, tzinfo=ZoneInfo("America/New_York")),
    )

    assert request is not None
    assert request.local_date == "2026-09-02"
    assert request.start_utc == datetime(2026, 9, 2, 4, tzinfo=UTC)
    assert request.end_utc == datetime(2026, 9, 3, 4, tzinfo=UTC)
    assert request.currencies == ()
    assert request.minimum_importance == 0


def test_parse_today_calendar_understands_natural_filters() -> None:
    request = parse_chat_calendar_request(
        "Show me high-impact news today for the US and Canada.",
        now=datetime(2026, 9, 2, 9, tzinfo=ZoneInfo("America/New_York")),
    )

    assert request is not None
    assert request.currencies == ("CAD", "USD")
    assert request.minimum_importance == 3
    assert request.impact_label == "high only"


def test_parse_today_calendar_accepts_plain_news_wording() -> None:
    assert parse_chat_calendar_request("What news is scheduled for today?") is not None


def test_historical_news_request_stays_in_the_agent_flow() -> None:
    assert (
        parse_chat_calendar_request("Show me previous CPI news history from today.")
        is None
    )


def test_chat_calendar_renderer_is_compact_and_does_not_expose_internal_tools(
    monkeypatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, force_terminal=False, width=72),
    )
    request = parse_chat_calendar_request(
        "Show me today's economic news.",
        now=datetime(2026, 9, 2, 9, tzinfo=ZoneInfo("America/New_York")),
    )
    assert request is not None
    event = SimpleNamespace(
        scheduled_at=datetime(2026, 9, 2, 12, 30, tzinfo=UTC),
        currency="USD",
        importance=3,
        title="Core PCE Price Index m/m",
        source="forex-factory",
    )

    summary = cli_module._render_chat_calendar(request, (event,))
    rendered = output.getvalue()

    assert "Trading Agent: Today's economic news" in rendered
    assert "08:30 EDT" in rendered
    assert "Core PCE Price Index m/m" in rendered
    assert "Forex Factory" in rendered
    assert "get_economic_calendar" not in rendered
    assert "requires explicit parameters" not in rendered
    assert summary.startswith("Displayed 1 stored economic event")
