"""Read-only Alpaca market-data connector for normalized quote and candle feeds."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import httpx

from app.market_data.contracts import Candle, Quote

ALPACA_STOCKS_BARS_PATH = "/v2/stocks/{symbol}/bars"
ALPACA_STOCKS_LATEST_QUOTE_PATH = "/v2/stocks/{symbol}/quotes/latest"
ALPACA_MAX_RESPONSE_BYTES: Final = 1_000_000


class AlpacaConnectorError(RuntimeError):
    pass


def _retry_delay(value: str | None, attempt: int) -> float:
    fallback = 0.25 * 2**attempt
    if not value:
        return fallback
    try:
        delay = float(value)
    except ValueError:
        return fallback
    if delay <= 0:
        return fallback
    return min(delay, 5.0)


def _normalize_timeframe(timeframe: str) -> str:
    text = timeframe.strip().upper()
    if not text:
        raise ValueError("timeframe is required")
    if text in {"1M", "M1", "1MIN", "1MINUTE"}:
        return "1Min"
    if text in {"5M", "M5", "5MIN", "5MINUTES"}:
        return "5Min"
    if text in {"15M", "M15", "15MIN", "15MINUTES"}:
        return "15Min"
    if text in {"30M", "M30", "30MIN", "30MINUTES"}:
        return "30Min"
    if text in {"1H", "H1", "1HOUR"}:
        return "1Hour"
    if text in {"2H", "H2", "2HOUR"}:
        return "2Hour"
    if text in {"4H", "H4", "4HOUR"}:
        return "4Hour"
    if text in {"1D", "D1", "DAY"}:
        return "1Day"
    raise ValueError(f"unsupported timeframe: {timeframe}")


def normalize_quote(
    payload: dict[str, Any],
    instrument: str,
    retrieved_at: datetime,
    source: str = "alpaca",
    venue: str = "ALPACA",
) -> Quote:
    ask = payload.get("ap")
    bid = payload.get("bp")
    if ask is None or bid is None:
        raise ValueError("Alpaca latest quote payload is missing ask or bid")
    market_time = payload.get("t")
    if market_time is None:
        raise ValueError("Alpaca quote payload is missing market time")
    return Quote(
        instrument=instrument,
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        market_time=datetime.fromisoformat(str(market_time).replace("Z", "+00:00")),
        retrieved_at=retrieved_at,
        source=source,
        venue=venue,
    )


def normalize_candle(
    payload: dict[str, Any],
    *,
    instrument: str,
    timeframe: str,
    retrieved_at: datetime,
    source: str = "alpaca",
    venue: str = "ALPACA",
) -> Candle:
    started_at = datetime.fromisoformat(str(payload["t"]).replace("Z", "+00:00"))
    if started_at.tzinfo is None:
        raise ValueError("Alpaca candle timestamps must be timezone-aware")
    return Candle(
        instrument=instrument,
        timeframe=timeframe,
        started_at=started_at,
        open=Decimal(str(payload["o"])),
        high=Decimal(str(payload["h"])),
        low=Decimal(str(payload["l"])),
        close=Decimal(str(payload["c"])),
        volume=Decimal(str(payload.get("v", 0))),
        complete=True,
        retrieved_at=retrieved_at,
        source=source,
        venue=venue,
    )


class AlpacaReadOnlyConnector:
    """Read-only data path for Alpaca stocks and crypto symbols."""

    name = "alpaca"
    venue = "ALPACA"

    def __init__(
        self,
        *,
        key_id: str,
        secret_key: str,
        base_url: str,
        timeout_seconds: float = 10.0,
        maximum_response_bytes: int = ALPACA_MAX_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not key_id or not secret_key:
            raise ValueError("Alpaca key id and secret are required")
        if maximum_response_bytes < 1:
            raise ValueError("Alpaca maximum response size must be positive")
        self.base_url = base_url
        self.maximum_response_bytes = maximum_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
                "accept": "application/json",
            },
        )

    async def _get_json(self, path: str, **params: str | int | None) -> dict[str, Any]:
        filtered = {key: value for key, value in params.items() if value is not None}
        for attempt in range(3):
            try:
                async with self._client.stream("GET", path, params=filtered) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            raise AlpacaConnectorError(
                                f"Alpaca request failed with status {response.status_code}"
                            )
                        await asyncio.sleep(
                            _retry_delay(response.headers.get("Retry-After"), attempt)
                        )
                        continue
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.maximum_response_bytes:
                            raise AlpacaConnectorError(
                                "Alpaca response exceeded the configured limit"
                            )
                        chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 2:
                    raise AlpacaConnectorError(
                        f"Alpaca transport failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.25 * 2**attempt)
                continue
            except httpx.HTTPStatusError as exc:
                raise AlpacaConnectorError(
                    f"Alpaca request failed with status {exc.response.status_code}"
                ) from exc
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise AlpacaConnectorError("Alpaca returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise AlpacaConnectorError("Alpaca returned an invalid payload")
            return payload
        raise AlpacaConnectorError("Alpaca request failed")

    async def latest_quote(self, instrument: str) -> Quote:
        symbol = instrument.upper().strip()
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(
            ALPACA_STOCKS_LATEST_QUOTE_PATH.format(symbol=symbol)
        )
        quote_payload = payload.get("quote")
        if not isinstance(quote_payload, dict):
            raise AlpacaConnectorError("Alpaca latest quote payload is malformed")
        return normalize_quote(
            payload=quote_payload,
            instrument=symbol,
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
        if count < 1 or count > 10_000:
            raise ValueError("Alpaca candle count must be between 1 and 10_000")
        symbol = instrument.upper().strip()
        normalized_timeframe = _normalize_timeframe(timeframe)
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(
            ALPACA_STOCKS_BARS_PATH.format(symbol=symbol),
            timeframe=normalized_timeframe,
            limit=str(count),
            feed="sip",
        )
        bars = payload.get("bars")
        if bars is None and symbol in payload:
            bars = payload[symbol]
            if isinstance(bars, dict):
                bars = bars.get("bars")
        if not isinstance(bars, list):
            raise AlpacaConnectorError("Alpaca bars payload is malformed")
        return tuple(
            normalize_candle(
                bar,
                instrument=symbol,
                timeframe=normalized_timeframe,
                retrieved_at=retrieved_at,
                source=self.name,
                venue=self.venue,
            )
            for bar in bars
            if (
                isinstance(bar, dict)
                and {"t", "o", "h", "l", "c", "v"}.issubset(bar)
            )
        )

    async def stream_quotes(self, instruments: Sequence[str]) -> AsyncIterator[Quote]:
        if not instruments:
            raise ValueError("at least one instrument is required")
        raise RuntimeError("Alpaca quote stream is unavailable in this release")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
