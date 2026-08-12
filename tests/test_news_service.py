from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import EconomicEvent
from app.news.contracts import CalendarEvent
from app.services.news import (
    economic_event_history,
    store_calendar_events,
    stored_economic_calendar,
)


def test_calendar_refresh_does_not_erase_stronger_stored_values(db_session) -> None:
    event = CalendarEvent(
        external_id="retain-release-values",
        scheduled_at=datetime(2026, 7, 29, 12, 30, tzinfo=UTC),
        timing_estimated=True,
        country="USD",
        currency="USD",
        category="Economic calendar",
        title="Advance GDP q/q",
        importance=3,
        actual="2.2%",
        forecast="2.1%",
        previous="2.0%",
        source_updated_at=None,
        source_url="https://example.test/calendar",
        retrieved_at=datetime(2026, 7, 29, 12, 31, tzinfo=UTC),
        source="test-calendar-retention",
    )
    store_calendar_events(db_session, (event,))

    store_calendar_events(
        db_session,
        (
            replace(
                event,
                actual=None,
                forecast=None,
                previous=None,
                retrieved_at=datetime(2026, 7, 29, 12, 32, tzinfo=UTC),
            ),
        ),
    )

    stored = db_session.scalar(
        select(EconomicEvent).where(
            EconomicEvent.source == event.source,
            EconomicEvent.source_event_id == event.external_id,
        )
    )
    assert stored is not None
    assert stored.actual == "2.2%"
    assert stored.forecast == "2.1%"
    assert stored.previous == "2.0%"
    assert stored.retrieved_at == datetime(2026, 7, 29, 12, 32, tzinfo=UTC)


def test_economic_event_history_returns_only_requested_past_releases(
    db_session,
) -> None:
    cutoff = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            EconomicEvent(
                source="history-test",
                source_event_id="core-pce-past",
                scheduled_at=datetime(2026, 6, 26, 12, 30, tzinfo=UTC),
                timing_estimated=False,
                country="United States",
                currency="USD",
                category="Inflation",
                title="Core PCE Price Index m/m",
                importance=3,
                actual="0.2%",
                forecast="0.2%",
                previous="0.1%",
                retrieved_at=cutoff,
            ),
            EconomicEvent(
                source="history-test",
                source_event_id="core-pce-future",
                scheduled_at=datetime(2026, 8, 27, 12, 30, tzinfo=UTC),
                timing_estimated=False,
                country="United States",
                currency="USD",
                category="Inflation",
                title="Core PCE Price Index m/m",
                importance=3,
                actual=None,
                forecast="0.2%",
                previous="0.2%",
                retrieved_at=cutoff,
            ),
            EconomicEvent(
                source="history-test",
                source_event_id="cpi-past",
                scheduled_at=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
                timing_estimated=False,
                country="United States",
                currency="USD",
                category="Inflation",
                title="CPI m/m",
                importance=3,
                actual="0.3%",
                forecast="0.2%",
                previous="0.1%",
                retrieved_at=cutoff,
            ),
        ]
    )
    db_session.commit()

    events = economic_event_history(
        db_session,
        "Core PCE",
        currency="usd",
        before=cutoff,
    )

    assert [event.source_event_id for event in events] == ["core-pce-past"]
    assert events[0].actual == "0.2%"


def test_stored_calendar_supports_country_names_and_impact_filters(
    db_session,
) -> None:
    retrieved_at = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
    db_session.add_all(
        [
            EconomicEvent(
                source="stored-calendar-test",
                source_event_id="usd-high",
                scheduled_at=datetime(2026, 7, 29, 12, 30, tzinfo=UTC),
                timing_estimated=False,
                country="USD",
                currency="USD",
                category="Growth",
                title="GDP",
                importance=3,
                retrieved_at=retrieved_at,
            ),
            EconomicEvent(
                source="stored-calendar-test",
                source_event_id="eur-high",
                scheduled_at=datetime(2026, 7, 29, 13, 0, tzinfo=UTC),
                timing_estimated=False,
                country="EUR",
                currency="EUR",
                category="Inflation",
                title="CPI",
                importance=3,
                retrieved_at=retrieved_at,
            ),
            EconomicEvent(
                source="stored-calendar-test",
                source_event_id="usd-low",
                scheduled_at=datetime(2026, 7, 29, 14, 0, tzinfo=UTC),
                timing_estimated=False,
                country="USD",
                currency="USD",
                category="Survey",
                title="Minor survey",
                importance=1,
                retrieved_at=retrieved_at,
            ),
        ]
    )
    db_session.commit()

    events = stored_economic_calendar(
        db_session,
        start=datetime(2026, 7, 29, tzinfo=UTC).date(),
        end=datetime(2026, 7, 29, tzinfo=UTC).date(),
        countries=("United States",),
        minimum_importance=2,
        source="stored-calendar-test",
    )

    assert [event.source_event_id for event in events] == ["usd-high"]
