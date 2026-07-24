from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Quote:
    instrument: str
    bid: Decimal
    ask: Decimal
    market_time: datetime
    retrieved_at: datetime
    source: str
    venue: str

    def __post_init__(self) -> None:
        _require_aware(self.market_time, "market_time")
        _require_aware(self.retrieved_at, "retrieved_at")
        if self.ask < self.bid:
            raise ValueError("ask cannot be below bid")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Candle:
    instrument: str
    timeframe: str
    started_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    complete: bool
    retrieved_at: datetime
    source: str
    venue: str

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        _require_aware(self.retrieved_at, "retrieved_at")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must contain the candle prices")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must contain the candle prices")
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True, slots=True)
class PositionState:
    external_id: str
    instrument: str
    net_quantity: Decimal
    average_price: Decimal | None
    unrealized_pnl: Decimal | None
    market_time: datetime
    retrieved_at: datetime
    source: str

    def __post_init__(self) -> None:
        _require_aware(self.market_time, "market_time")
        _require_aware(self.retrieved_at, "retrieved_at")


@dataclass(frozen=True, slots=True)
class AccountState:
    external_account_id: str
    currency: str
    balance: Decimal
    equity: Decimal
    margin_used: Decimal | None
    margin_available: Decimal | None
    market_time: datetime
    retrieved_at: datetime
    source: str

    def __post_init__(self) -> None:
        _require_aware(self.market_time, "market_time")
        _require_aware(self.retrieved_at, "retrieved_at")


@dataclass(frozen=True, slots=True)
class BrokerEvent:
    external_id: str
    event_type: str
    occurred_at: datetime
    instrument: str | None
    external_order_id: str | None
    external_trade_id: str | None
    quantity: Decimal | None
    price: Decimal | None
    realized_pnl: Decimal | None
    source: str

    def __post_init__(self) -> None:
        _require_aware(self.occurred_at, "occurred_at")


class MarketDataConnector(Protocol):
    """Read-only market data. Implementations must never expose order methods."""

    name: str
    venue: str

    async def latest_quote(self, instrument: str) -> Quote: ...

    async def candles(
        self,
        instrument: str,
        timeframe: str,
        *,
        count: int,
    ) -> Sequence[Candle]: ...

    def stream_quotes(self, instruments: Sequence[str]) -> AsyncIterator[Quote]: ...


class ReadOnlyBrokerConnector(Protocol):
    """Broker state and transaction history, intentionally excluding write methods."""

    name: str

    async def account(self) -> AccountState: ...

    async def positions(self) -> Sequence[PositionState]: ...

    async def events_since(
        self, cursor: str | None
    ) -> tuple[Sequence[BrokerEvent], str | None]: ...
