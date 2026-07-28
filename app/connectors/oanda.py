"""OANDA v20 normalization boundary.

This module deliberately contains no order-create, replace, cancel, or close methods.
HTTP and streaming clients can be injected later without leaking credentials into agent
prompts or journal records.
"""

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.market_data.contracts import (
    AccountState,
    BrokerEvent,
    BrokerTradeEffect,
    Candle,
    PositionState,
    Quote,
    SyncPage,
)

PRACTICE_REST_URL = "https://api-fxpractice.oanda.com"
LIVE_REST_URL = "https://api-fxtrade.oanda.com"
PRACTICE_STREAM_URL = "https://stream-fxpractice.oanda.com"
LIVE_STREAM_URL = "https://stream-fxtrade.oanda.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TRANSACTION_ID_RANGE = 1_000


class OandaConnectorError(RuntimeError):
    pass


def _retry_delay(value: str | None, attempt: int) -> float:
    fallback = 0.25 * 2**attempt
    if not value:
        return fallback
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    if not math.isfinite(delay) or delay < 0:
        return fallback
    return min(delay, 5.0)


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


def _optional_decimal(payload: dict[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    return Decimal(str(value)) if value is not None else None


def _trade_effect(
    payload: dict[str, Any],
    effect: str,
) -> BrokerTradeEffect | None:
    trade_id = payload.get("tradeID")
    units = payload.get("units")
    if trade_id is None or units is None:
        return None
    return BrokerTradeEffect(
        external_trade_id=str(trade_id),
        effect=effect,
        quantity=Decimal(str(units)),
        realized_pnl=_optional_decimal(payload, "realizedPL"),
    )


def normalize_transaction(payload: dict[str, Any]) -> BrokerEvent:
    effects: list[BrokerTradeEffect] = []
    opened = payload.get("tradeOpened")
    reduced = payload.get("tradeReduced")
    if isinstance(opened, dict) and (effect := _trade_effect(opened, "opened")):
        effects.append(effect)
    if isinstance(reduced, dict) and (effect := _trade_effect(reduced, "reduced")):
        effects.append(effect)
    for closed in payload.get("tradesClosed") or []:
        if isinstance(closed, dict) and (effect := _trade_effect(closed, "closed")):
            effects.append(effect)
    nested_trade_id = next(
        (
            str(item["tradeID"])
            for item in (
                opened,
                reduced,
                *((payload.get("tradesClosed") or [])),
            )
            if isinstance(item, dict) and item.get("tradeID") is not None
        ),
        None,
    )
    primary_trade_id = (
        next(
            (
                effect.external_trade_id
                for effect in effects
                if effect.effect == "opened"
            ),
            None,
        )
        or next(
            (
                effect.external_trade_id
                for effect in effects
                if effect.effect == "reduced"
            ),
            None,
        )
        or (effects[0].external_trade_id if effects else nested_trade_id)
    )
    return BrokerEvent(
        external_id=str(payload["id"]),
        event_type=str(payload["type"]).lower(),
        occurred_at=datetime.fromisoformat(str(payload["time"]).replace("Z", "+00:00")),
        instrument=payload.get("instrument"),
        external_order_id=payload.get("orderID"),
        external_trade_id=payload.get("tradeID") or primary_trade_id,
        quantity=_optional_decimal(payload, "units"),
        price=_optional_decimal(payload, "price"),
        realized_pnl=_optional_decimal(payload, "pl"),
        source="oanda-v20",
        commission=_optional_decimal(payload, "commission"),
        financing=_optional_decimal(payload, "financing"),
        guaranteed_execution_fee=_optional_decimal(
            payload,
            "guaranteedExecutionFee",
        ),
        half_spread_cost=_optional_decimal(payload, "halfSpreadCost"),
        trade_effects=tuple(effects),
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
        maximum_response_bytes: int = MAX_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
        stream_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token or not account_id:
            raise ValueError("OANDA token and account id are required")
        if environment not in {"practice", "live"}:
            raise ValueError("OANDA environment must be practice or live")
        if maximum_response_bytes < 1:
            raise ValueError("OANDA maximum response size must be positive")
        self.account_id = account_id
        self.environment = environment
        self.maximum_response_bytes = maximum_response_bytes
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
                async with self._client.stream("GET", path, params=params) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            raise OandaConnectorError(
                                f"OANDA request failed with status {response.status_code}"
                            )
                        await asyncio.sleep(
                            _retry_delay(response.headers.get("retry-after"), attempt)
                        )
                        continue
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.maximum_response_bytes:
                            raise OandaConnectorError(
                                "OANDA response exceeded the configured limit"
                            )
                        chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
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
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OandaConnectorError("OANDA returned invalid JSON") from exc
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
    ) -> SyncPage:
        if cursor is None:
            payload = await self._get_json(f"/v3/accounts/{self.account_id}/summary")
            return SyncPage(
                events=(),
                cursor_before=None,
                cursor_after=str(payload["lastTransactionID"]),
                has_more=False,
                coverage="baseline",
            )
        if not cursor.isdigit():
            raise ValueError("OANDA event cursor must contain only digits")
        summary = await self._get_json(f"/v3/accounts/{self.account_id}/summary")
        latest = int(summary["lastTransactionID"])
        current = int(cursor)
        if current > latest:
            raise ValueError("OANDA event cursor is ahead of the account transaction stream")
        if current == latest:
            return SyncPage(
                events=(),
                cursor_before=cursor,
                cursor_after=cursor,
                has_more=False,
                coverage="complete" if current == 0 else "incremental",
            )
        page_end = min(current + MAX_TRANSACTION_ID_RANGE, latest)
        payload = await self._get_json(
            f"/v3/accounts/{self.account_id}/transactions/idrange",
            params={"from": current + 1, "to": page_end},
        )
        raw_events = payload.get("transactions") or []
        if not isinstance(raw_events, list):
            raise OandaConnectorError("OANDA returned an invalid transaction list")
        if len(raw_events) > MAX_TRANSACTION_ID_RANGE:
            raise OandaConnectorError("OANDA returned too many transactions for one page")
        events = tuple(normalize_transaction(item) for item in raw_events)
        has_more = page_end < latest
        return SyncPage(
            events=events,
            cursor_before=cursor,
            cursor_after=str(page_end),
            has_more=has_more,
            coverage="complete" if current == 0 and not has_more else "incremental",
        )

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
