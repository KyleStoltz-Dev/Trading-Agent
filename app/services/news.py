import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import EconomicEvent, NewsItem
from app.news.contracts import CalendarEvent, NewsHeadline

_COUNTRY_CURRENCIES = {
    "australia": "AUD",
    "canada": "CAD",
    "china": "CNY",
    "euro area": "EUR",
    "eurozone": "EUR",
    "france": "EUR",
    "germany": "EUR",
    "italy": "EUR",
    "japan": "JPY",
    "new zealand": "NZD",
    "switzerland": "CHF",
    "united kingdom": "GBP",
    "united states": "USD",
}


def stored_economic_calendar(
    db: Session,
    *,
    start: date,
    end: date,
    countries: Sequence[str],
    minimum_importance: int,
    source: str | None = None,
) -> tuple[EconomicEvent, ...]:
    """Read a bounded calendar window from retained provider evidence."""
    if start > end:
        raise ValueError("calendar start must not be after end")
    if not 0 <= minimum_importance <= 3:
        raise ValueError("minimum importance must be between 0 and 3")
    start_at = datetime.combine(start, time.min, tzinfo=UTC)
    end_at = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    statement = (
        select(EconomicEvent)
        .where(
            EconomicEvent.scheduled_at >= start_at,
            EconomicEvent.scheduled_at < end_at,
            EconomicEvent.importance >= minimum_importance,
        )
        .order_by(EconomicEvent.scheduled_at, EconomicEvent.importance.desc())
        .limit(500)
    )
    if source:
        statement = statement.where(EconomicEvent.source == source)
    country_tokens = {
        value.strip().upper()
        for value in countries
        if value.strip()
    }
    currency_tokens = {
        _COUNTRY_CURRENCIES.get(value.strip().casefold(), value.strip().upper())
        for value in countries
        if value.strip()
    }
    if country_tokens or currency_tokens:
        statement = statement.where(
            or_(
                func.upper(EconomicEvent.country).in_(country_tokens),
                func.upper(EconomicEvent.currency).in_(currency_tokens),
            )
        )
    return tuple(db.scalars(statement))


def economic_event_history(
    db: Session,
    event_query: str,
    *,
    currency: str | None = None,
    limit: int = 10,
    before: datetime | None = None,
) -> tuple[EconomicEvent, ...]:
    """Return previously stored releases for one human-readable event query."""
    normalized_query = " ".join(event_query.split())
    if len(normalized_query) < 2:
        raise ValueError("event query must contain at least two characters")
    if not 1 <= limit <= 50:
        raise ValueError("history limit must be between 1 and 50")
    cutoff = before or datetime.now(UTC)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("history cutoff must be timezone-aware")
    statement = (
        select(EconomicEvent)
        .where(
            EconomicEvent.title.icontains(normalized_query, autoescape=True),
            EconomicEvent.scheduled_at < cutoff,
        )
        .order_by(EconomicEvent.scheduled_at.desc(), EconomicEvent.retrieved_at.desc())
        .limit(limit)
    )
    normalized_currency = (currency or "").strip().upper()
    if normalized_currency:
        statement = statement.where(EconomicEvent.currency == normalized_currency)
    return tuple(db.scalars(statement))


def store_calendar_events(
    db: Session,
    events: list[CalendarEvent] | tuple[CalendarEvent, ...],
) -> int:
    stored = 0
    for event in events:
        values = {
            "scheduled_at": event.scheduled_at,
            "timing_estimated": event.timing_estimated,
            "country": event.country,
            "currency": event.currency,
            "category": event.category,
            "title": event.title,
            "importance": event.importance,
            "actual": event.actual,
            "forecast": event.forecast,
            "previous": event.previous,
            "source_updated_at": event.source_updated_at,
            "retrieved_at": event.retrieved_at,
            "source_url": event.source_url,
        }
        inserted = db.scalar(
            insert(EconomicEvent)
            .values(
                id=uuid.uuid4(),
                source=event.source,
                source_event_id=event.external_id,
                **values,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    EconomicEvent.source,
                    EconomicEvent.source_event_id,
                ]
            )
            .returning(EconomicEvent.id)
        )
        if inserted is not None:
            stored += 1
        else:
            # Some free schedule feeds stop publishing a value after a release or
            # never publish actuals at all. Do not erase stronger evidence that a
            # later enrichment source already stored.
            retained_values = {
                key: value
                for key, value in values.items()
                if key not in {"actual", "forecast", "previous"} or value is not None
            }
            db.execute(
                update(EconomicEvent)
                .where(
                    EconomicEvent.source == event.source,
                    EconomicEvent.source_event_id == event.external_id,
                )
                .values(**retained_values)
            )
    db.commit()
    return stored


def store_news_items(db: Session, items: list[NewsHeadline] | tuple[NewsHeadline, ...]) -> int:
    stored = 0
    for item in items:
        content_hash = hashlib.sha256(
            f"{item.title}\n{item.summary or ''}".encode()
        ).hexdigest()
        values = {
            "title": item.title,
            "summary": item.summary,
            "country": item.country,
            "category": item.category,
            "symbol": item.symbol,
            "importance": item.importance,
            "published_at": item.published_at,
            "retrieved_at": item.retrieved_at,
            "source_url": item.source_url,
            "content_hash": content_hash,
        }
        inserted = db.scalar(
            insert(NewsItem)
            .values(
                id=uuid.uuid4(),
                source=item.source,
                source_item_id=item.external_id,
                **values,
            )
            .on_conflict_do_nothing(
                index_elements=[NewsItem.source, NewsItem.source_item_id]
            )
            .returning(NewsItem.id)
        )
        if inserted is not None:
            stored += 1
        else:
            db.execute(
                update(NewsItem)
                .where(
                    NewsItem.source == item.source,
                    NewsItem.source_item_id == item.external_id,
                )
                .values(**values)
            )
    db.commit()
    return stored
