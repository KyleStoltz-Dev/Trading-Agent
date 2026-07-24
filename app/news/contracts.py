from dataclasses import dataclass
from datetime import datetime


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    external_id: str
    scheduled_at: datetime
    timing_estimated: bool
    country: str
    currency: str | None
    category: str | None
    title: str
    importance: int
    actual: str | None
    forecast: str | None
    previous: str | None
    source_updated_at: datetime | None
    source_url: str | None
    retrieved_at: datetime
    source: str

    def __post_init__(self) -> None:
        _require_aware(self.scheduled_at, "scheduled_at")
        _require_aware(self.retrieved_at, "retrieved_at")
        if self.source_updated_at is not None:
            _require_aware(self.source_updated_at, "source_updated_at")
        if not 0 <= self.importance <= 3:
            raise ValueError("importance must be between 0 and 3")


@dataclass(frozen=True, slots=True)
class NewsHeadline:
    external_id: str
    title: str
    summary: str | None
    country: str | None
    category: str | None
    symbol: str | None
    importance: int
    published_at: datetime
    source_url: str | None
    retrieved_at: datetime
    source: str

    def __post_init__(self) -> None:
        _require_aware(self.published_at, "published_at")
        _require_aware(self.retrieved_at, "retrieved_at")
        if not 0 <= self.importance <= 3:
            raise ValueError("importance must be between 0 and 3")
