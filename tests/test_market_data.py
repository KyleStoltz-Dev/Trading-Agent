from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.connectors.mt5 import normalize_rate, normalize_tick
from app.connectors.oanda import normalize_candle, normalize_quote, normalize_transaction
from app.market_data.cache import LiveMarketCache, StaleMarketDataError
from app.market_data.contracts import (
    Candle,
    MarketDataConnector,
    Quote,
    ReadOnlyBrokerConnector,
)

NOW = datetime(2026, 7, 23, 16, 0, tzinfo=UTC)


def test_live_quote_cache_rejects_stale_data() -> None:
    cache = LiveMarketCache()
    quote = Quote(
        instrument="XAU_USD",
        bid=Decimal("2399.50"),
        ask=Decimal("2400.00"),
        market_time=NOW,
        retrieved_at=NOW,
        source="oanda-v20",
        venue="OANDA",
    )
    cache.put_quote(quote)

    assert cache.quote("oanda-v20", "XAU_USD", max_age=timedelta(seconds=2), now=NOW) == quote
    with pytest.raises(StaleMarketDataError, match="older"):
        cache.quote(
            "oanda-v20",
            "XAU_USD",
            max_age=timedelta(seconds=2),
            now=NOW + timedelta(seconds=3),
        )


def test_live_candle_cache_replaces_partial_candle_and_stays_bounded() -> None:
    cache = LiveMarketCache(candle_capacity=2)

    def candle(started_at: datetime, close: str, complete: bool = True) -> Candle:
        return Candle(
            instrument="XAU_USD",
            timeframe="M5",
            started_at=started_at,
            open=Decimal("2398"),
            high=Decimal("2402"),
            low=Decimal("2397"),
            close=Decimal(close),
            volume=Decimal("10"),
            complete=complete,
            retrieved_at=NOW,
            source="oanda-v20",
            venue="OANDA",
        )

    cache.put_candles([candle(NOW, "2399", False)])
    cache.put_candles([candle(NOW, "2400", True)])
    cache.put_candles(
        [
            candle(NOW + timedelta(minutes=5), "2401"),
            candle(NOW + timedelta(minutes=10), "2402"),
        ]
    )

    stored = cache.candles("oanda-v20", "XAU_USD", "M5")
    assert len(stored) == 2
    assert [item.close for item in stored] == [Decimal("2401"), Decimal("2402")]


def test_oanda_payloads_are_normalized_without_persisting_ticks() -> None:
    quote = normalize_quote(
        {
            "instrument": "XAU_USD",
            "time": "2026-07-23T16:00:00Z",
            "bids": [{"price": "2399.50"}],
            "asks": [{"price": "2400.00"}],
        },
        retrieved_at=NOW,
    )
    candle = normalize_candle(
        {
            "time": "2026-07-23T15:55:00Z",
            "mid": {"o": "2398", "h": "2401", "l": "2397", "c": "2400"},
            "volume": 81,
            "complete": True,
        },
        instrument="XAU_USD",
        timeframe="M5",
        retrieved_at=NOW,
    )
    event = normalize_transaction(
        {
            "id": "9001",
            "type": "ORDER_FILL",
            "time": "2026-07-23T16:00:00Z",
            "instrument": "XAU_USD",
            "orderID": "8001",
            "tradeID": "7001",
            "units": "-2.5",
            "price": "2400",
            "pl": "0",
        }
    )

    assert quote.spread == Decimal("0.50")
    assert candle.close == Decimal("2400")
    assert event.external_id == "9001"
    assert event.quantity == Decimal("-2.5")


def test_mt5_terminal_values_are_normalized() -> None:
    tick = normalize_tick(
        "XAUUSD",
        SimpleNamespace(bid=2399.5, ask=2400.0, time_msc=NOW.timestamp() * 1_000),
        retrieved_at=NOW,
    )
    rate = normalize_rate(
        "XAUUSD",
        "M5",
        {
            "time": int(NOW.timestamp()),
            "open": 2398,
            "high": 2401,
            "low": 2397,
            "close": 2400,
            "tick_volume": 81,
            "real_volume": 0,
        },
        retrieved_at=NOW,
    )

    assert tick.midpoint == Decimal("2399.75")
    assert rate.volume == Decimal("81")


def test_connector_protocols_do_not_define_execution_methods() -> None:
    forbidden = {
        "place_order",
        "submit_order",
        "modify_order",
        "cancel_order",
        "close_position",
    }
    protocol_names = set(vars(MarketDataConnector)) | set(vars(ReadOnlyBrokerConnector))

    assert forbidden.isdisjoint(protocol_names)
