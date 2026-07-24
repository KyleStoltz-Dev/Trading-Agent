from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock

from app.market_data.contracts import AccountState, Candle, PositionState, Quote


class StaleMarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceStatus:
    source: str
    connected: bool
    last_message_at: datetime | None
    last_heartbeat_at: datetime | None
    last_error: str | None
    rejected_out_of_order: int


class LiveMarketCache:
    """Bounded process memory for live data; it does not persist ticks to PostgreSQL."""

    def __init__(self, candle_capacity: int = 2_000) -> None:
        if candle_capacity < 1:
            raise ValueError("candle_capacity must be positive")
        self._candle_capacity = candle_capacity
        self._quotes: dict[tuple[str, str], Quote] = {}
        self._candles: dict[tuple[str, str, str], deque[Candle]] = defaultdict(
            lambda: deque(maxlen=self._candle_capacity)
        )
        self._positions: tuple[PositionState, ...] = ()
        self._account: AccountState | None = None
        self._source_status: dict[str, SourceStatus] = {}
        self._lock = RLock()

    def _update_source(
        self,
        source: str,
        *,
        connected: bool | None = None,
        message_at: datetime | None = None,
        heartbeat_at: datetime | None = None,
        error: str | None = None,
        rejected: bool = False,
    ) -> None:
        previous = self._source_status.get(
            source,
            SourceStatus(source, False, None, None, None, 0),
        )
        self._source_status[source] = SourceStatus(
            source=source,
            connected=previous.connected if connected is None else connected,
            last_message_at=message_at or previous.last_message_at,
            last_heartbeat_at=heartbeat_at or previous.last_heartbeat_at,
            last_error=(
                None
                if error == ""
                else error
                if error is not None
                else previous.last_error
            ),
            rejected_out_of_order=previous.rejected_out_of_order + int(rejected),
        )

    def mark_connected(self, source: str, at: datetime | None = None) -> None:
        with self._lock:
            self._update_source(
                source,
                connected=True,
                message_at=at or datetime.now(UTC),
                error="",
            )

    def mark_heartbeat(self, source: str, at: datetime | None = None) -> None:
        timestamp = at or datetime.now(UTC)
        with self._lock:
            self._update_source(
                source,
                connected=True,
                message_at=timestamp,
                heartbeat_at=timestamp,
                error="",
            )

    def mark_disconnected(self, source: str, error_type: str) -> None:
        with self._lock:
            self._update_source(
                source,
                connected=False,
                message_at=datetime.now(UTC),
                error=error_type,
            )

    def put_quote(self, quote: Quote) -> bool:
        with self._lock:
            key = (quote.source, quote.instrument)
            existing = self._quotes.get(key)
            if existing is not None and quote.market_time < existing.market_time:
                self._update_source(quote.source, rejected=True)
                return False
            self._quotes[key] = quote
            self._update_source(
                quote.source,
                connected=True,
                message_at=quote.retrieved_at,
                error="",
            )
            return True

    def quote(
        self,
        source: str,
        instrument: str,
        *,
        max_age: timedelta,
        now: datetime | None = None,
    ) -> Quote:
        with self._lock:
            quote = self._quotes.get((source, instrument))
        if quote is None:
            raise StaleMarketDataError(f"no quote for {source}:{instrument}")
        current = now or datetime.now(UTC)
        if current - quote.retrieved_at > max_age:
            raise StaleMarketDataError(f"quote for {source}:{instrument} is older than {max_age}")
        return quote

    def put_candles(self, candles: Sequence[Candle]) -> None:
        with self._lock:
            for candle in candles:
                key = (candle.source, candle.instrument, candle.timeframe)
                buffer = self._candles[key]
                if buffer and buffer[-1].started_at == candle.started_at:
                    buffer[-1] = candle
                elif buffer and candle.started_at < buffer[-1].started_at:
                    self._update_source(candle.source, rejected=True)
                else:
                    buffer.append(candle)
                    self._update_source(
                        candle.source,
                        connected=True,
                        message_at=candle.retrieved_at,
                        error="",
                    )

    def candles(self, source: str, instrument: str, timeframe: str) -> tuple[Candle, ...]:
        with self._lock:
            return tuple(self._candles[(source, instrument, timeframe)])

    def put_broker_state(
        self,
        account: AccountState,
        positions: Sequence[PositionState],
    ) -> None:
        with self._lock:
            self._account = account
            self._positions = tuple(positions)

    def broker_state(self) -> tuple[AccountState | None, tuple[PositionState, ...]]:
        with self._lock:
            return self._account, self._positions

    def source_status(self, source: str) -> SourceStatus:
        with self._lock:
            return self._source_status.get(
                source,
                SourceStatus(source, False, None, None, None, 0),
            )
