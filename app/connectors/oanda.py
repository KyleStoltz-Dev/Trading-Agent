"""OANDA v20 normalization boundary.

This module deliberately contains no order-create, replace, cancel, or close methods.
HTTP and streaming clients can be injected later without leaking credentials into agent
prompts or journal records.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.market_data.contracts import (
    AccountState,
    BrokerEvent,
    Candle,
    PositionState,
    Quote,
)

PRACTICE_REST_URL = "https://api-fxpractice.oanda.com"
LIVE_REST_URL = "https://api-fxtrade.oanda.com"
PRACTICE_STREAM_URL = "https://stream-fxpractice.oanda.com"
LIVE_STREAM_URL = "https://stream-fxtrade.oanda.com"


class OandaConnectorError(RuntimeError):
    pass


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


class OandaReadOnlyConnector:
    """Authenticated OANDA v20 reads and streams with no order methods."""

    name = "oanda-v20"
    venue = "OANDA"

    def __init__(
        self,
        *,
        token: str,
        account_id: str,
        environment: str = "practice",
        timeout_seconds: float = 10,
        client: httpx.AsyncClient | None = None,
        stream_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token or not account_id:
            raise ValueError("OANDA token and account id are required")
        if environment not in {"practice", "live"}:
            raise ValueError("OANDA environment must be practice or live")
        self.account_id = account_id
        self.environment = environment
        self.last_heartbeat_at: datetime | None = None
        self._heartbeat_handler: Callable[[datetime], None] | None = None
        headers = {"Authorization": f"Bearer {token}"}
        rest_url = PRACTICE_REST_URL if environment == "practice" else LIVE_REST_URL
        stream_url = (
            PRACTICE_STREAM_URL if environment == "practice" else LIVE_STREAM_URL
        )
        timeout = httpx.Timeout(timeout_seconds)
        self._owns_client = client is None
        self._owns_stream_client = stream_client is None
        self._client = client or httpx.AsyncClient(
            base_url=rest_url,
            headers=headers,
            timeout=timeout,
        )
        self._stream_client = stream_client or httpx.AsyncClient(
            base_url=stream_url,
            headers=headers,
            timeout=httpx.Timeout(
                connect=timeout_seconds,
                read=None,
                write=timeout_seconds,
                pool=timeout_seconds,
            ),
        )

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = await self._client.get(path, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 2:
                        raise OandaConnectorError(
                            f"OANDA request failed with status {response.status_code}"
                        )
                    retry_after = response.headers.get("retry-after")
                    delay = min(float(retry_after), 5.0) if retry_after else 0.25 * 2**attempt
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise OandaConnectorError("OANDA returned an invalid JSON object")
                return payload
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 2:
                    raise OandaConnectorError(
                        f"OANDA transport failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.25 * 2**attempt)
            except httpx.HTTPStatusError as exc:
                raise OandaConnectorError(
                    f"OANDA request failed with status {exc.response.status_code}"
                ) from exc
        raise OandaConnectorError("OANDA request failed")

    async def latest_quote(self, instrument: str) -> Quote:
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": instrument},
        )
        prices = payload.get("prices") or []
        if len(prices) != 1:
            raise OandaConnectorError(
                f"OANDA returned {len(prices)} prices for {instrument}"
            )
        return normalize_quote(prices[0], retrieved_at=retrieved_at)

    async def candles(
        self,
        instrument: str,
        timeframe: str,
        *,
        count: int,
    ) -> Sequence[Candle]:
        if count < 1 or count > 5_000:
            raise ValueError("OANDA candle count must be between 1 and 5000")
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(
            f"/v3/instruments/{instrument}/candles",
            params={"granularity": timeframe, "count": count, "price": "M"},
        )
        return tuple(
            normalize_candle(
                item,
                instrument=instrument,
                timeframe=timeframe,
                retrieved_at=retrieved_at,
            )
            for item in payload.get("candles") or []
        )

    async def account(self) -> AccountState:
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(f"/v3/accounts/{self.account_id}/summary")
        account = payload["account"]
        return AccountState(
            external_account_id=str(account["id"]),
            currency=str(account["currency"]),
            balance=Decimal(str(account["balance"])),
            equity=Decimal(str(account["NAV"])),
            margin_used=Decimal(str(account["marginUsed"])),
            margin_available=Decimal(str(account["marginAvailable"])),
            market_time=retrieved_at,
            retrieved_at=retrieved_at,
            source=self.name,
        )

    async def positions(self) -> Sequence[PositionState]:
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(f"/v3/accounts/{self.account_id}/openPositions")
        positions = []
        for item in payload.get("positions") or []:
            long_units = Decimal(str(item["long"]["units"]))
            short_units = Decimal(str(item["short"]["units"]))
            net_units = long_units + short_units
            dominant = item["long"] if net_units >= 0 else item["short"]
            average_price = dominant.get("averagePrice")
            positions.append(
                PositionState(
                    external_id=str(item["instrument"]),
                    instrument=str(item["instrument"]),
                    net_quantity=net_units,
                    average_price=(
                        Decimal(str(average_price))
                        if average_price is not None
                        else None
                    ),
                    unrealized_pnl=Decimal(str(item["unrealizedPL"])),
                    market_time=retrieved_at,
                    retrieved_at=retrieved_at,
                    source=self.name,
                )
            )
        return tuple(positions)

    async def events_since(
        self,
        cursor: str | None,
    ) -> tuple[Sequence[BrokerEvent], str | None]:
        if cursor is None:
            payload = await self._get_json(f"/v3/accounts/{self.account_id}/summary")
            return (), str(payload["lastTransactionID"])
        payload = await self._get_json(
            f"/v3/accounts/{self.account_id}/transactions/sinceid",
            params={"id": cursor},
        )
        events = tuple(
            normalize_transaction(item) for item in payload.get("transactions") or []
        )
        return events, str(payload.get("lastTransactionID") or cursor)

    async def stream_quotes(
        self,
        instruments: Sequence[str],
    ) -> AsyncIterator[Quote]:
        if not instruments:
            raise ValueError("at least one instrument is required")
        path = f"/v3/accounts/{self.account_id}/pricing/stream"
        params = {"instruments": ",".join(instruments), "snapshot": "true"}
        try:
            async with self._stream_client.stream("GET", path, params=params) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    payload = json.loads(line)
                    retrieved_at = datetime.now(UTC)
                    if payload.get("type") == "HEARTBEAT":
                        self.last_heartbeat_at = retrieved_at
                        if self._heartbeat_handler is not None:
                            self._heartbeat_handler(retrieved_at)
                        continue
                    if payload.get("type") == "PRICE":
                        yield normalize_quote(payload, retrieved_at=retrieved_at)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise OandaConnectorError(
                f"OANDA pricing stream failed: {type(exc).__name__}"
            ) from exc

    def set_heartbeat_handler(
        self,
        handler: Callable[[datetime], None] | None,
    ) -> None:
        self._heartbeat_handler = handler

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._owns_stream_client:
            await self._stream_client.aclose()
