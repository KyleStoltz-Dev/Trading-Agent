from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_finite(value: Decimal | None, field: str) -> None:
    """Reject NaN/Infinity at the provider-normalization boundary."""
    if value is not None and not value.is_finite():
        raise ValueError(f"{field} must be finite")


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
        _require_finite(self.bid, "bid")
        _require_finite(self.ask, "ask")
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
        _require_finite(self.open, "open")
        _require_finite(self.high, "high")
        _require_finite(self.low, "low")
        _require_finite(self.close, "close")
        _require_finite(self.volume, "volume")
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
        _require_finite(self.net_quantity, "net_quantity")
        _require_finite(self.average_price, "average_price")
        _require_finite(self.unrealized_pnl, "unrealized_pnl")


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
        _require_finite(self.balance, "balance")
        _require_finite(self.equity, "equity")
        _require_finite(self.margin_used, "margin_used")
        _require_finite(self.margin_available, "margin_available")


@dataclass(frozen=True, slots=True)
class BrokerTradeEffect:
    """One provider-normalized lifecycle effect contained in a broker transaction."""

    external_trade_id: str
    effect: Literal["opened", "reduced", "closed"]
    quantity: Decimal
    realized_pnl: Decimal | None = None

    def __post_init__(self) -> None:
        _require_finite(self.quantity, "trade_effect.quantity")
        _require_finite(self.realized_pnl, "trade_effect.realized_pnl")


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
    commission: Decimal | None = None
    financing: Decimal | None = None
    guaranteed_execution_fee: Decimal | None = None
    half_spread_cost: Decimal | None = None
    trade_effects: tuple[BrokerTradeEffect, ...] = ()
    infer_trade_open: bool = True

    def __post_init__(self) -> None:
        _require_aware(self.occurred_at, "occurred_at")
        _require_finite(self.quantity, "quantity")
        _require_finite(self.price, "price")
        _require_finite(self.realized_pnl, "realized_pnl")
        _require_finite(self.commission, "commission")
        _require_finite(self.financing, "financing")
        _require_finite(self.guaranteed_execution_fee, "guaranteed_execution_fee")
        _require_finite(self.half_spread_cost, "half_spread_cost")


SyncCoverage = Literal["baseline", "incremental", "complete"]


@dataclass(frozen=True, slots=True)
class SyncPage:
    """One broker event page with explicit cursor and ledger coverage semantics."""

    events: tuple[BrokerEvent, ...]
    cursor_before: str | None
    cursor_after: str | None
    has_more: bool
    coverage: SyncCoverage


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

    async def events_since(self, cursor: str | None) -> SyncPage: ...
