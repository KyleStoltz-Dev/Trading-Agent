from collections import defaultdict, deque
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from threading import RLock

from app.market_data.contracts import AccountState, Candle, PositionState, Quote


class StaleMarketDataError(RuntimeError):
    pass


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
        self._lock = RLock()

    def put_quote(self, quote: Quote) -> None:
        with self._lock:
            self._quotes[(quote.source, quote.instrument)] = quote

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
                else:
                    buffer.append(candle)

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
