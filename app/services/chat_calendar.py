"""Deterministic intent and date handling for routine calendar chat requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EconomicEvent

_CURRENCY_NAMES = {
    "australia": "AUD",
    "canada": "CAD",
    "china": "CNY",
    "euro area": "EUR",
    "eurozone": "EUR",
    "japan": "JPY",
    "new zealand": "NZD",
    "switzerland": "CHF",
    "united kingdom": "GBP",
    "uk": "GBP",
    "united states": "USD",
    "us": "USD",
}
_CURRENCY_CODES = frozenset(
    {"AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "JPY", "NZD", "USD"}
)


@dataclass(frozen=True)
class ChatCalendarRequest:
    local_date: str
    local_timezone: tzinfo
    start_utc: datetime
    end_utc: datetime
    currencies: tuple[str, ...]
    minimum_importance: int

    @property
    def impact_label(self) -> str:
        return {0: "all impacts", 1: "low and above", 2: "medium and high", 3: "high only"}[
            self.minimum_importance
        ]


def parse_chat_calendar_request(
    message: str,
    *,
    now: datetime | None = None,
) -> ChatCalendarRequest | None:
    """Recognize a simple request for today's economic calendar, not broad news analysis."""
    normalized = " ".join(message.casefold().replace("’", "'").split())
    explicit_calendar = any(
        phrase in normalized
        for phrase in (
            "economic calendar",
            "calendar events",
        )
    )
    routine_news_request = "news" in normalized and any(
        phrase in normalized for phrase in ("show", "what", "which", "any", "list")
    )
    asks_for_calendar = explicit_calendar or routine_news_request
    if "today" not in normalized or not asks_for_calendar:
        return None
    if any(term in normalized for term in ("history", "historical", "previous", "prior")):
        return None

    current = now or datetime.now().astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("calendar clock must include a timezone")
    local_timezone = current.tzinfo
    start_local = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)

    currencies = {
        code
        for code in _CURRENCY_CODES
        if re.search(rf"\b{code.casefold()}\b", normalized)
    }
    currencies.update(
        code
        for name, code in _CURRENCY_NAMES.items()
        if re.search(rf"\b{re.escape(name)}\b", normalized)
    )
    if "high" in normalized and "medium" not in normalized:
        minimum_importance = 3
    elif "medium" in normalized:
        minimum_importance = 2
    elif "low" in normalized:
        minimum_importance = 1
    else:
        minimum_importance = 0
    return ChatCalendarRequest(
        local_date=start_local.date().isoformat(),
        local_timezone=local_timezone,
        start_utc=start_local.astimezone(UTC),
        end_utc=end_local.astimezone(UTC),
        currencies=tuple(sorted(currencies)),
        minimum_importance=minimum_importance,
    )


def calendar_events_for_chat(
    db: Session,
    request: ChatCalendarRequest,
) -> tuple[EconomicEvent, ...]:
    statement = (
        select(EconomicEvent)
        .where(
            EconomicEvent.scheduled_at >= request.start_utc,
            EconomicEvent.scheduled_at < request.end_utc,
            EconomicEvent.importance >= request.minimum_importance,
        )
        .order_by(EconomicEvent.scheduled_at, EconomicEvent.importance.desc())
    )
    if request.currencies:
        statement = statement.where(EconomicEvent.currency.in_(request.currencies))
    return tuple(db.scalars(statement))
