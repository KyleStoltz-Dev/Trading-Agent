"""MetaTrader 5 normalization boundary.

The official Python package requires a reachable MT5 terminal. This reference adapter keeps
terminal-specific dictionaries outside the domain model and intentionally exposes no calls
to ``order_send`` or other write operations.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.market_data.contracts import Candle, Quote


def normalize_tick(
    symbol: str,
    tick: Any,
    *,
    retrieved_at: datetime,
    venue: str = "MT5",
) -> Quote:
    return Quote(
        instrument=symbol,
        bid=Decimal(str(tick.bid)),
        ask=Decimal(str(tick.ask)),
        market_time=datetime.fromtimestamp(int(tick.time_msc) / 1_000, tz=UTC),
        retrieved_at=retrieved_at,
        source="mt5-terminal",
        venue=venue,
    )


def normalize_rate(
    symbol: str,
    timeframe: str,
    rate: Any,
    *,
    retrieved_at: datetime,
    venue: str = "MT5",
) -> Candle:
    if hasattr(rate, "_asdict"):
        values = rate._asdict()
    elif isinstance(rate, Mapping):
        values = rate
    elif getattr(rate, "dtype", None) is not None and rate.dtype.names:
        values = {name: rate[name] for name in rate.dtype.names}
    else:
        raise TypeError("MT5 rate must be a mapping or structured array row")
    return Candle(
        instrument=symbol,
        timeframe=timeframe,
        started_at=datetime.fromtimestamp(int(values["time"]), tz=UTC),
        open=Decimal(str(values["open"])),
        high=Decimal(str(values["high"])),
        low=Decimal(str(values["low"])),
        close=Decimal(str(values["close"])),
        volume=Decimal(str(values.get("real_volume") or values.get("tick_volume"))),
        complete=True,
        retrieved_at=retrieved_at,
        source="mt5-terminal",
        venue=venue,
    )
