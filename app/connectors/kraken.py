"""Read-only Kraken public market data normalization boundary."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx

from app.market_data.contracts import Candle, Quote

KRAKEN_PUBLIC_REST_URL: Final = "https://api.kraken.com"
MAX_RESPONSE_BYTES: Final = 1_000_000


class KrakenConnectorError(RuntimeError):
    pass


def _normalize_symbol(value: str) -> str:
    candidate = "".join(char for char in value.upper().strip() if char.isalnum())
    if not candidate:
        raise ValueError("instrument is required")
    if candidate.startswith("BTC"):
        return f"XBT{candidate[3:]}"
    return candidate


def _parse_timeframe(timeframe: str) -> int:
    """Return the candle interval in minutes."""
    text = timeframe.strip().upper()
    if not text:
        raise ValueError("timeframe is required")
    if text in {"1M", "1"}:
        return 1
    if text in {"2M", "2"}:
        return 2
    if text in {"5M", "M5"}:
        return 5
    if text in {"15M", "M15"}:
        return 15
    if text in {"30M", "M30"}:
        return 30
    if text in {"1H", "H1"}:
        return 60
    if text in {"4H", "H4"}:
        return 240
    if text in {"12H", "H12"}:
        return 720
    if text in {"1D", "D1", "D"}:
        return 1440
    if text in {"1W", "W1", "W"}:
        return 10080
    if text in {"1MONTH", "1MTH", "1MO"}:
        return 43200
    raise ValueError(f"unsupported timeframe: {timeframe}")


def normalize_quote(
    payload: dict[str, Any],
    symbol: str,
    retrieved_at: datetime,
    source: str = "kraken",
    venue: str = "KRAKEN",
) -> Quote:
    bid = payload.get("b")
    ask = payload.get("a")
    if not bid or not ask:
        raise ValueError("Kraken ticker requires bid/ask arrays")
    bid_price = bid[0]
    ask_price = ask[0]
    if bid_price is None or ask_price is None:
        raise ValueError("Kraken ticker missing bid/ask prices")
    return Quote(
        instrument=symbol,
        bid=Decimal(str(bid_price)),
        ask=Decimal(str(ask_price)),
        market_time=retrieved_at,
        retrieved_at=retrieved_at,
        source=source,
        venue=venue,
    )


def normalize_candle(
    payload: tuple[str, str, str, str, str, str, str, str],
    *,
    instrument: str,
    timeframe: str,
    retrieved_at: datetime,
    source: str = "kraken",
    venue: str = "KRAKEN",
    interval_minutes: int,
) -> Candle:
    started_at = datetime.fromtimestamp(int(float(payload[0])), tz=UTC)
    complete = started_at + timedelta(minutes=interval_minutes) < datetime.now(UTC)
    return Candle(
        instrument=instrument,
        timeframe=timeframe,
        started_at=started_at,
        open=Decimal(str(payload[1])),
        high=Decimal(str(payload[2])),
        low=Decimal(str(payload[3])),
        close=Decimal(str(payload[4])),
        volume=Decimal(str(payload[6])),
        complete=complete,
        retrieved_at=retrieved_at,
        source=source,
        venue=venue,
    )


class KrakenReadOnlyConnector:
    """Read-only market data from Kraken public endpoints."""

    name = "kraken"
    venue = "KRAKEN"

    def __init__(
        self,
        *,
        base_url: str = KRAKEN_PUBLIC_REST_URL,
        timeout_seconds: float = 10,
        maximum_response_bytes: int = MAX_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if maximum_response_bytes < 1:
            raise ValueError("Kraken maximum response size must be positive")
        self.base_url = base_url
        self.maximum_response_bytes = maximum_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"User-Agent": "Trading-Agent/0.1 (+read-only market data)"},
        )

    async def _get_json(self, path: str, **params: str | int | None) -> dict[str, Any]:
        filtered = {key: value for key, value in params.items() if value is not None}
        for attempt in range(3):
            try:
                async with self._client.stream("GET", path, params=filtered) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            raise KrakenConnectorError(
                                f"Kraken request failed with status {response.status_code}"
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
                            raise KrakenConnectorError(
                                "Kraken response exceeded the configured limit"
                            )
                        chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 2:
                    raise KrakenConnectorError(
                        f"Kraken transport failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.25 * 2**attempt)
                continue
            except ValueError as exc:
                raise KrakenConnectorError("Kraken returned invalid JSON") from exc
            except httpx.HTTPStatusError as exc:
                raise KrakenConnectorError(
                    f"Kraken request failed with status "
                    f"{exc.response.status_code}"
                ) from exc
            if payload.get("error"):
                sample = hashlib.sha256(
                    str(payload.get("error")).encode()
                ).hexdigest()[:8]
                raise KrakenConnectorError(
                    f"Kraken returned API errors (example={sample})"
                )
            if not isinstance(payload.get("result"), dict):
                raise KrakenConnectorError("Kraken returned an invalid response")
            return payload
        raise KrakenConnectorError("Kraken request failed")

    async def latest_quote(self, instrument: str) -> Quote:
        symbol = _normalize_symbol(instrument)
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json("/0/public/Ticker", pair=symbol)
        ticker_payload = payload.get("result", {}).get(symbol)
        if not isinstance(ticker_payload, dict):
            # Some symbols are keyed differently (e.g. XBTUSD -> XXBTZUSD). Use first result.
            if not payload.get("result"):
                raise KrakenConnectorError("Kraken ticker payload is empty")
            first = next(iter(payload["result"].values()))
            if not isinstance(first, dict):
                raise KrakenConnectorError("Kraken ticker payload is malformed")
            ticker_payload = first
        return normalize_quote(
            payload=ticker_payload,
            symbol=instrument,
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
        if count < 1 or count > 5000:
            raise ValueError("Kraken candle count must be between 1 and 5000")
        interval = _parse_timeframe(timeframe)
        symbol = _normalize_symbol(instrument)
        retrieved_at = datetime.now(UTC)
        payload = await self._get_json(
            "/0/public/OHLC",
            pair=symbol,
            interval=str(interval),
            count=str(count),
        )
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise KrakenConnectorError("Kraken OHLC payload is malformed")
        if len(result) != 2:
            # result may include `last` in addition to candle key.
            if set(result.keys()) == {"last"}:
                raise KrakenConnectorError("Kraken OHLC payload is missing pair data")
        pair_key = next((key for key in result if key != "last"), None)
        if pair_key is None:
            raise KrakenConnectorError("Kraken OHLC payload is missing pair data")
        rows = result.get(pair_key)
        if not isinstance(rows, list):
            raise KrakenConnectorError("Kraken OHLC rows are malformed")
        return tuple(
            normalize_candle(
                tuple(item),
                instrument=instrument,
                timeframe=timeframe,
                retrieved_at=retrieved_at,
                source=self.name,
                venue=self.venue,
                interval_minutes=interval,
            )
            for item in rows
            if isinstance(item, list)
            and len(item) >= 8
        )

    async def stream_quotes(self, instruments: Sequence[str]) -> AsyncIterator[Quote]:
        if not instruments:
            raise ValueError("at least one instrument is required")
        raise RuntimeError("Kraken read-only stream is unavailable in this release")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _retry_delay(value: str | None, attempt: int) -> float:
    fallback = 0.25 * 2**attempt
    if not value:
        return fallback
    try:
        delay = float(value)
        if delay <= 0:
            return fallback
        return min(delay, 5.0)
    except ValueError:
        return fallback
