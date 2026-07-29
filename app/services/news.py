import hashlib
import uuid

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import EconomicEvent, NewsItem
from app.news.contracts import CalendarEvent, NewsHeadline


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
