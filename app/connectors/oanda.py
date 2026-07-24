"""OANDA v20 normalization boundary.

This module deliberately contains no order-create, replace, cancel, or close methods.
HTTP and streaming clients can be injected later without leaking credentials into agent
prompts or journal records.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.market_data.contracts import BrokerEvent, Candle, Quote


def normalize_quote(
    payload: dict[str, Any],
    *,
    retrieved_at: datetime,
    venue: str = "OANDA",
) -> Quote:
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    if not bids or not asks:
        raise ValueError("OANDA price payload requires at least one bid and ask")
    return Quote(
        instrument=str(payload["instrument"]),
        bid=Decimal(str(bids[0]["price"])),
        ask=Decimal(str(asks[0]["price"])),
        market_time=datetime.fromisoformat(str(payload["time"]).replace("Z", "+00:00")),
        retrieved_at=retrieved_at,
        source="oanda-v20",
        venue=venue,
    )


def normalize_candle(
    payload: dict[str, Any],
    *,
    instrument: str,
    timeframe: str,
    retrieved_at: datetime,
    venue: str = "OANDA",
) -> Candle:
    prices = payload.get("mid")
    if not prices:
        raise ValueError("OANDA candle payload requires midpoint prices")
    return Candle(
        instrument=instrument,
        timeframe=timeframe,
        started_at=datetime.fromisoformat(str(payload["time"]).replace("Z", "+00:00")),
        open=Decimal(str(prices["o"])),
        high=Decimal(str(prices["h"])),
        low=Decimal(str(prices["l"])),
        close=Decimal(str(prices["c"])),
        volume=Decimal(str(payload["volume"])),
        complete=bool(payload["complete"]),
        retrieved_at=retrieved_at,
        source="oanda-v20",
        venue=venue,
    )


def normalize_transaction(payload: dict[str, Any]) -> BrokerEvent:
    trade = payload.get("tradeOpened") or payload.get("tradeReduced")
    closed = payload.get("tradesClosed") or []
    nested_trade_id = trade.get("tradeID") if trade else None
    if nested_trade_id is None and closed:
        nested_trade_id = closed[0].get("tradeID")
    return BrokerEvent(
        external_id=str(payload["id"]),
        event_type=str(payload["type"]).lower(),
        occurred_at=datetime.fromisoformat(str(payload["time"]).replace("Z", "+00:00")),
        instrument=payload.get("instrument"),
        external_order_id=payload.get("orderID"),
        external_trade_id=payload.get("tradeID") or nested_trade_id,
        quantity=Decimal(str(payload["units"])) if payload.get("units") is not None else None,
        price=Decimal(str(payload["price"])) if payload.get("price") is not None else None,
        realized_pnl=(Decimal(str(payload["pl"])) if payload.get("pl") is not None else None),
        source="oanda-v20",
    )
