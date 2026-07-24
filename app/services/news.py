import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EconomicEvent, NewsItem
from app.news.contracts import CalendarEvent, NewsHeadline


def store_calendar_events(
    db: Session,
    events: list[CalendarEvent] | tuple[CalendarEvent, ...],
) -> int:
    stored = 0
    for event in events:
        existing = db.scalar(
            select(EconomicEvent).where(
                EconomicEvent.source == event.source,
                EconomicEvent.source_event_id == event.external_id,
            )
        )
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
        if existing is None:
            db.add(
                EconomicEvent(
                    source=event.source,
                    source_event_id=event.external_id,
                    **values,
                )
            )
            stored += 1
        else:
            for key, value in values.items():
                setattr(existing, key, value)
    db.commit()
    return stored


def store_news_items(db: Session, items: list[NewsHeadline] | tuple[NewsHeadline, ...]) -> int:
    stored = 0
    for item in items:
        existing = db.scalar(
            select(NewsItem).where(
                NewsItem.source == item.source,
                NewsItem.source_item_id == item.external_id,
            )
        )
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
        if existing is None:
            db.add(
                NewsItem(
                    source=item.source,
                    source_item_id=item.external_id,
                    **values,
                )
            )
            stored += 1
        else:
            for key, value in values.items():
                setattr(existing, key, value)
    db.commit()
    return stored
