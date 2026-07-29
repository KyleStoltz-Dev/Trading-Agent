from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import EconomicEvent
from app.news.contracts import CalendarEvent
from app.services.news import store_calendar_events


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
