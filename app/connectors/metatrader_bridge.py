"""Read-only MetaTrader bridge client.

MetaTrader terminals are not embedded in the agent process. A small service running beside
MT4 or MT5 exposes a deliberately narrow HTTP contract for market data, account state,
positions, and execution history. This client contains no order endpoint or generic request
method.
"""

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

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

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
_TIMEFRAME_PATTERN = re.compile(r"^[A-Za-z0-9]{1,12}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class MetaTraderBridgeError(RuntimeError):
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


def _object_items(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    items = payload.get(key)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise MetaTraderBridgeError(f"MetaTrader bridge {key} must be a list of objects")
    return items


def _timestamp(value: Any, *, milliseconds: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, int | float):
        divisor = 1_000 if milliseconds else 1
        parsed = datetime.fromtimestamp(float(value) / divisor, tz=UTC)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("timestamp cannot be empty")
        try:
            numeric = float(stripped)
        except ValueError:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        else:
            divisor = 1_000 if milliseconds else 1
            parsed = datetime.fromtimestamp(numeric / divisor, tz=UTC)
    else:
        raise ValueError("timestamp must be ISO-8601 or Unix time")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _market_timestamp(payload: Mapping[str, Any]) -> datetime:
    if payload.get("time_msc") is not None:
        return _timestamp(payload["time_msc"], milliseconds=True)
    value = payload.get("market_time", payload.get("time"))
    return _timestamp(value)


def _optional_decimal(payload: Mapping[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    return Decimal(str(value)) if value is not None else None


def _identifier(
    value: Any,
    *,
    field: str,
    maximum_length: int,
) -> str:
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > maximum_length
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"bridge returned an invalid {field}")
    return normalized


def _symbol(payload: Mapping[str, Any]) -> str:
    value = str(payload.get("instrument") or payload.get("symbol") or "").strip()
    if not _SYMBOL_PATTERN.fullmatch(value):
        raise ValueError("bridge returned an invalid instrument symbol")
    return value


def normalize_bridge_quote(
    payload: Mapping[str, Any],
    *,
    retrieved_at: datetime,
    source: str,
    venue: str,
) -> Quote:
    return Quote(
        instrument=_symbol(payload),
        bid=Decimal(str(payload["bid"])),
        ask=Decimal(str(payload["ask"])),
        market_time=_market_timestamp(payload),
        retrieved_at=retrieved_at,
        source=source,
        venue=venue,
    )


def normalize_bridge_candle(
    payload: Mapping[str, Any],
    *,
    instrument: str,
    timeframe: str,
    retrieved_at: datetime,
    source: str,
    venue: str,
) -> Candle:
    return Candle(
        instrument=instrument,
        timeframe=timeframe,
        started_at=_market_timestamp(payload),
        open=Decimal(str(payload["open"])),
        high=Decimal(str(payload["high"])),
        low=Decimal(str(payload["low"])),
        close=Decimal(str(payload["close"])),
        volume=_optional_decimal(payload, "volume"),
        complete=bool(payload.get("complete", True)),
        retrieved_at=retrieved_at,
        source=source,
        venue=venue,
    )


def normalize_bridge_account(
    payload: Mapping[str, Any],
    *,
    retrieved_at: datetime,
    source: str,
) -> AccountState:
    return AccountState(
        external_account_id=_identifier(
            payload["account_id"],
            field="account id",
            maximum_length=160,
        ),
        currency=_identifier(
            payload["currency"],
            field="currency",
            maximum_length=12,
        ),
        balance=Decimal(str(payload["balance"])),
        equity=Decimal(str(payload["equity"])),
        margin_used=_optional_decimal(payload, "margin_used"),
        margin_available=_optional_decimal(payload, "margin_available"),
        market_time=_market_timestamp(payload),
        retrieved_at=retrieved_at,
        source=source,
    )


def normalize_bridge_position(
    payload: Mapping[str, Any],
    *,
    retrieved_at: datetime,
    source: str,
) -> PositionState:
    return PositionState(
        external_id=_identifier(
            payload["position_id"],
            field="position id",
            maximum_length=160,
        ),
        instrument=_symbol(payload),
        net_quantity=Decimal(str(payload["net_quantity"])),
        average_price=_optional_decimal(payload, "average_price"),
        unrealized_pnl=_optional_decimal(payload, "unrealized_pnl"),
        market_time=_market_timestamp(payload),
        retrieved_at=retrieved_at,
        source=source,
    )


def normalize_bridge_event(
    payload: Mapping[str, Any],
    *,
    source: str,
) -> BrokerEvent:
    effects: list[BrokerTradeEffect] = []
    for raw_effect in payload.get("trade_effects") or ():
        if not isinstance(raw_effect, Mapping):
            raise ValueError("bridge trade effect must be an object")
        effect = str(raw_effect["effect"])
        if effect not in {"opened", "reduced", "closed"}:
            raise ValueError("bridge trade effect is invalid")
        effects.append(
            BrokerTradeEffect(
                external_trade_id=_identifier(
                    raw_effect["external_trade_id"],
                    field="trade id",
                    maximum_length=160,
                ),
                effect=effect,  # type: ignore[arg-type]
                quantity=Decimal(str(raw_effect["quantity"])),
                realized_pnl=_optional_decimal(raw_effect, "realized_pnl"),
            )
        )
    instrument = payload.get("instrument") or payload.get("symbol")
    normalized_instrument = (
        _symbol({"instrument": instrument}) if instrument is not None else None
    )
    return BrokerEvent(
        external_id=_identifier(
            payload["event_id"],
            field="event id",
            maximum_length=160,
        ),
        event_type=_identifier(
            payload["event_type"],
            field="event type",
            maximum_length=64,
        ).casefold(),
        occurred_at=_market_timestamp(payload),
        instrument=normalized_instrument,
        external_order_id=(
            _identifier(
                payload["order_id"],
                field="order id",
                maximum_length=160,
            )
            if payload.get("order_id") is not None
            else None
        ),
        external_trade_id=(
            _identifier(
                payload["trade_id"],
                field="trade id",
                maximum_length=160,
            )
            if payload.get("trade_id") is not None
            else None
        ),
        quantity=_optional_decimal(payload, "quantity"),
        price=_optional_decimal(payload, "price"),
        realized_pnl=_optional_decimal(payload, "realized_pnl"),
        commission=_optional_decimal(payload, "commission"),
        financing=_optional_decimal(payload, "financing"),
        guaranteed_execution_fee=_optional_decimal(
            payload,
            "guaranteed_execution_fee",
        ),
        half_spread_cost=_optional_decimal(payload, "half_spread_cost"),
        source=source,
        trade_effects=tuple(effects),
        infer_trade_open=bool(payload.get("infer_trade_open", False)),
    )


class MetaTraderReadOnlyBridgeConnector:
    """Fixed-endpoint MT4/MT5 reads with bounded responses and no execution API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        account_id: str,
        platform: str,
        timeout_seconds: float = 10,
        poll_interval_seconds: float = 1,
        maximum_response_bytes: int = 2_000_000,
        allow_insecure_remote: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if platform not in {"mt4", "mt5"}:
            raise ValueError("MetaTrader platform must be mt4 or mt5")
        if len(token) < 32 or not account_id:
            raise ValueError(
                "MetaTrader bridge requires an account id and a token of at least "
                "32 characters"
            )
        if poll_interval_seconds < 0.1:
            raise ValueError("MetaTrader polling interval must be at least 0.1 seconds")
        if maximum_response_bytes < 1:
            raise ValueError("MetaTrader maximum response size must be positive")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MetaTrader bridge URL must be an absolute HTTP(S) URL")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "MetaTrader bridge URL must contain only scheme, host, and optional port"
            )
        if (
            parsed.scheme != "https"
            and parsed.hostname.casefold() not in _LOOPBACK_HOSTS
            and not allow_insecure_remote
        ):
            raise ValueError(
                "remote MetaTrader bridges require HTTPS; explicitly allow insecure "
                "remote HTTP only on a trusted private network"
            )
        self.platform = platform
        self.account_id = account_id
        self.name = f"metatrader-{platform}-bridge"
        self.venue = platform.upper()
        self.poll_interval_seconds = poll_interval_seconds
        self.maximum_response_bytes = maximum_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    @staticmethod
    def _validated_symbol(value: str) -> str:
        symbol = value.strip()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("instrument must be a broker-style symbol")
        return symbol

    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(3):
            try:
                async with self._client.stream("GET", path, params=params) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            raise MetaTraderBridgeError(
                                f"MetaTrader bridge request failed with status "
                                f"{response.status_code}"
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
                            raise MetaTraderBridgeError(
                                "MetaTrader bridge response exceeded the configured limit"
                            )
                        chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
                if not isinstance(payload, dict):
                    raise MetaTraderBridgeError(
                        "MetaTrader bridge returned an invalid JSON object"
                    )
                return payload
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 2:
                    raise MetaTraderBridgeError(
                        f"MetaTrader bridge transport failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.25 * 2**attempt)
            except httpx.HTTPStatusError as exc:
                raise MetaTraderBridgeError(
                    f"MetaTrader bridge request failed with status "
                    f"{exc.response.status_code}"
                ) from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise MetaTraderBridgeError(
                    "MetaTrader bridge returned invalid JSON"
                ) from exc
        raise MetaTraderBridgeError("MetaTrader bridge request failed")

    def _verify_account(self, account_id: str) -> None:
        if account_id != self.account_id:
            raise MetaTraderBridgeError(
                "MetaTrader bridge account does not match METATRADER_ACCOUNT_ID"
            )

    async def health(self) -> dict[str, Any]:
        payload = await self._get_json("/v1/health")
        if payload.get("read_only") is not True:
            raise MetaTraderBridgeError("MetaTrader bridge did not attest read-only mode")
        if payload.get("terminal_connected") is not True:
            raise MetaTraderBridgeError("MetaTrader terminal is not connected")
        if str(payload.get("platform", "")).casefold() != self.platform:
            raise MetaTraderBridgeError(
                "MetaTrader bridge platform does not match METATRADER_PLATFORM"
            )
        self._verify_account(str(payload.get("account_id", "")))
        return payload

    async def latest_quote(self, instrument: str) -> Quote:
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(
            "/v1/quote",
            params={"instrument": self._validated_symbol(instrument)},
        )
        return normalize_bridge_quote(
            payload,
            retrieved_at=retrieved_at,
            source=self.name,
            venue=self.venue,
        )

    async def candles(
        self,
        instrument: str,
        timeframe: str,
        *,
        count: int,
    ) -> Sequence[Candle]:
        if count < 1 or count > 5_000:
            raise ValueError("MetaTrader candle count must be between 1 and 5000")
        if not _TIMEFRAME_PATTERN.fullmatch(timeframe):
            raise ValueError("MetaTrader timeframe is invalid")
        instrument = self._validated_symbol(instrument)
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(
            "/v1/candles",
            params={
                "instrument": instrument,
                "timeframe": timeframe,
                "count": count,
            },
        )
        items = _object_items(payload, "candles")
        if len(items) > count:
            raise MetaTraderBridgeError(
                "MetaTrader bridge returned more candles than requested"
            )
        return tuple(
            normalize_bridge_candle(
                item,
                instrument=instrument,
                timeframe=timeframe,
                retrieved_at=retrieved_at,
                source=self.name,
                venue=self.venue,
            )
            for item in items
        )

    async def account(self) -> AccountState:
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json("/v1/account")
        account = normalize_bridge_account(
            payload,
            retrieved_at=retrieved_at,
            source=self.name,
        )
        self._verify_account(account.external_account_id)
        return account

    async def positions(self) -> Sequence[PositionState]:
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json("/v1/positions")
        self._verify_account(str(payload.get("account_id", "")))
        items = _object_items(payload, "positions")
        return tuple(
            normalize_bridge_position(
                item,
                retrieved_at=retrieved_at,
                source=self.name,
            )
            for item in items
        )

    async def events_since(
        self,
        cursor: str | None,
    ) -> SyncPage:
        if cursor is not None and (
            len(cursor) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in cursor)
        ):
            raise ValueError("MetaTrader event cursor is invalid")
        params = {"cursor": cursor} if cursor is not None else None
        payload = await self._get_json("/v1/events", params=params)
        self._verify_account(str(payload.get("account_id", "")))
        items = _object_items(payload, "events")
        if len(items) > 5_000:
            raise MetaTraderBridgeError("MetaTrader bridge returned too many events")
        events = tuple(
            normalize_bridge_event(item, source=self.name)
            for item in items
        )
        next_cursor = payload.get("next_cursor")
        normalized_cursor = (
            _identifier(
                next_cursor,
                field="event cursor",
                maximum_length=200,
            )
            if next_cursor is not None
            else cursor
        )
        has_more = payload.get("has_more", False)
        if not isinstance(has_more, bool):
            raise MetaTraderBridgeError("MetaTrader bridge has_more must be a boolean")
        baseline_only = payload.get("baseline_only", cursor is None)
        if not isinstance(baseline_only, bool):
            raise MetaTraderBridgeError(
                "MetaTrader bridge baseline_only must be a boolean"
            )
        return SyncPage(
            events=events,
            cursor_before=cursor,
            cursor_after=normalized_cursor,
            has_more=has_more,
            coverage="baseline" if baseline_only else "incremental",
        )

    async def stream_quotes(
        self,
        instruments: Sequence[str],
    ) -> AsyncIterator[Quote]:
        symbols = tuple(self._validated_symbol(item) for item in instruments)
        if not symbols:
            raise ValueError("at least one instrument is required")
        while True:
            for instrument in symbols:
                yield await self.latest_quote(instrument)
            await asyncio.sleep(self.poll_interval_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
