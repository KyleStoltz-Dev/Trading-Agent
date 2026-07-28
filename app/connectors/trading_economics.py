import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.news.contracts import CalendarEvent, NewsHeadline


class TradingEconomicsError(RuntimeError):
    pass


def _utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith("http") else f"https://tradingeconomics.com{value}"


class TradingEconomicsReadOnlyConnector:
    name = "trading-economics"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10,
        maximum_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not api_key:
            raise ValueError("Trading Economics API key is required")
        if maximum_response_bytes < 1:
            raise ValueError("Trading Economics maximum response size must be positive")
        self.maximum_response_bytes = maximum_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.tradingeconomics.com",
            headers={"Authorization": api_key},
            timeout=timeout_seconds,
        )

    async def _get(self, path: str, params: dict[str, str | int]) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                async with self._client.stream(
                    "GET",
                    path,
                    params={**params, "f": "json"},
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            raise TradingEconomicsError(
                                "Trading Economics request failed with status "
                                f"{response.status_code}"
                            )
                        retry_after = response.headers.get("Retry-After", "")
                        try:
                            delay = min(max(float(retry_after), 0), 5)
                        except ValueError:
                            delay = 0.25 * 2**attempt
                        await asyncio.sleep(delay)
                        continue
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.maximum_response_bytes:
                            raise TradingEconomicsError(
                                "Trading Economics response exceeded the configured limit"
                            )
                        chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 2:
                    raise TradingEconomicsError(
                        "Trading Economics transport failed: "
                        f"{type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.25 * 2**attempt)
                continue
            except httpx.HTTPStatusError as exc:
                raise TradingEconomicsError(
                    "Trading Economics request failed with status "
                    f"{exc.response.status_code}"
                ) from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise TradingEconomicsError(
                    "Trading Economics returned invalid JSON"
                ) from exc
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                raise TradingEconomicsError(
                    "Trading Economics returned an invalid response"
                )
            return payload
        raise TradingEconomicsError("Trading Economics request failed")

    async def calendar(
        self,
        *,
        start: date,
        end: date,
        countries: Sequence[str],
        minimum_importance: int = 2,
    ) -> Sequence[CalendarEvent]:
        if start > end:
            raise ValueError("calendar start must not be after end")
        if not 0 <= minimum_importance <= 3:
            raise ValueError("minimum importance must be between 0 and 3")
        country_path = ",".join(countries) if countries else "All"
        payload = await self._get(
            f"/calendar/country/{country_path}/{start.isoformat()}/{end.isoformat()}",
            {"importance": minimum_importance},
        )
        retrieved_at = datetime.now(UTC)
        events = []
        for item in payload:
            scheduled_at = _utc_datetime(item.get("Date"))
            external_id = item.get("CalendarId") or item.get("CalendarID")
            if scheduled_at is None or external_id is None:
                continue
            events.append(
                CalendarEvent(
                    external_id=str(external_id),
                    scheduled_at=scheduled_at,
                    timing_estimated=str(item.get("DateSpan", "0")) != "0",
                    country=str(item.get("Country") or "unknown"),
                    currency=item.get("Currency"),
                    category=item.get("Category"),
                    title=str(item.get("Event") or item.get("Category") or "event"),
                    importance=int(item.get("Importance") or 0),
                    actual=item.get("Actual"),
                    forecast=item.get("Forecast"),
                    previous=item.get("Previous"),
                    source_updated_at=_utc_datetime(item.get("LastUpdate")),
                    source_url=item.get("SourceURL"),
                    retrieved_at=retrieved_at,
                    source=self.name,
                )
            )
        return tuple(events)

    async def news(
        self,
        *,
        country: str | None = None,
        limit: int = 50,
    ) -> Sequence[NewsHeadline]:
        if not 1 <= limit <= 250:
            raise ValueError("news limit must be between 1 and 250")
        path = f"/news/country/{country}" if country else "/news"
        payload = await self._get(path, {})
        retrieved_at = datetime.now(UTC)
        headlines = []
        for item in payload[:limit]:
            published_at = _utc_datetime(item.get("Date"))
            external_id = item.get("Id") or item.get("ID")
            if published_at is None or external_id is None:
                continue
            headlines.append(
                NewsHeadline(
                    external_id=str(external_id),
                    title=str(item.get("Title") or "untitled"),
                    summary=item.get("Description"),
                    country=item.get("Country"),
                    category=item.get("Category"),
                    symbol=item.get("Symbol"),
                    importance=int(item.get("Importance") or 0),
                    published_at=published_at,
                    source_url=_absolute_url(item.get("Url")),
                    retrieved_at=retrieved_at,
                    source=self.name,
                )
            )
        return tuple(headlines)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
