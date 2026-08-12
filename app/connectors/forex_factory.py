import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.news.contracts import CalendarEvent, NewsHeadline

FOREX_FACTORY_WEEKLY_FEED = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
)
FOREX_FACTORY_CALENDAR_URL = "https://www.forexfactory.com/calendar"

_IMPACT = {
    "holiday": 0,
    "non-economic": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}
_COUNTRY_CURRENCIES = {
    "australia": "AUD",
    "canada": "CAD",
    "china": "CNY",
    "euro area": "EUR",
    "eurozone": "EUR",
    "france": "EUR",
    "germany": "EUR",
    "italy": "EUR",
    "japan": "JPY",
    "new zealand": "NZD",
    "switzerland": "CHF",
    "united kingdom": "GBP",
    "united states": "USD",
}


class ForexFactoryError(RuntimeError):
    pass


def _country_filter(countries: Sequence[str]) -> frozenset[str]:
    values: set[str] = set()
    for country in countries:
        normalized = country.strip()
        if not normalized:
            continue
        values.add(_COUNTRY_CURRENCIES.get(normalized.casefold(), normalized.upper()))
    return frozenset(values)


def _event_id(item: dict[str, Any], scheduled_at: datetime) -> str:
    identity = "\n".join(
        (
            str(item.get("country") or "").strip().upper(),
            str(item.get("title") or "").strip(),
            scheduled_at.astimezone(UTC).isoformat(),
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


class ForexFactoryReadOnlyConnector:
    """Read the public Forex Factory weekly calendar export without browser automation."""

    name = "forex-factory"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10,
        maximum_response_bytes: int = 1_000_000,
    ) -> None:
        if maximum_response_bytes < 1:
            raise ValueError("Forex Factory maximum response size must be positive")
        self.maximum_response_bytes = maximum_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Accept": "application/json",
                "User-Agent": "Trading-Agent/0.1 (+read-only economic calendar)",
            },
            follow_redirects=False,
        )

    async def _get(self) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                async with self._client.stream(
                    "GET",
                    FOREX_FACTORY_WEEKLY_FEED,
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 2:
                            retry_after = response.headers.get("Retry-After")
                            suffix = (
                                f"; retry after {retry_after} seconds"
                                if retry_after
                                else ""
                            )
                            raise ForexFactoryError(
                                "Forex Factory calendar is temporarily unavailable"
                                f"{suffix}"
                            )
                        retry_after = response.headers.get("Retry-After", "")
                        try:
                            delay = min(max(float(retry_after), 0), 2)
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
                            raise ForexFactoryError(
                                "Forex Factory response exceeded the configured limit"
                            )
                        chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 2:
                    raise ForexFactoryError(
                        "Forex Factory calendar could not be reached"
                    ) from exc
                await asyncio.sleep(0.25 * 2**attempt)
                continue
            except httpx.HTTPStatusError as exc:
                raise ForexFactoryError(
                    "Forex Factory calendar request failed with status "
                    f"{exc.response.status_code}"
                ) from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ForexFactoryError(
                    "Forex Factory returned invalid calendar data"
                ) from exc
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                raise ForexFactoryError(
                    "Forex Factory returned an invalid calendar response"
                )
            return payload
        raise ForexFactoryError("Forex Factory calendar request failed")

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
        currencies = _country_filter(countries)
        retrieved_at = datetime.now(UTC)
        events: list[CalendarEvent] = []
        for item in await self._get():
            try:
                scheduled_at = datetime.fromisoformat(
                    str(item.get("date") or "").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if scheduled_at.tzinfo is None:
                continue
            currency = str(item.get("country") or "").strip().upper()
            importance = _IMPACT.get(
                str(item.get("impact") or "").strip().casefold(),
                0,
            )
            scheduled_date = scheduled_at.date()
            if (
                not start <= scheduled_date <= end
                or (currencies and currency not in currencies)
                or importance < minimum_importance
            ):
                continue
            title = str(item.get("title") or "").strip()
            if not title or not currency:
                continue
            events.append(
                CalendarEvent(
                    external_id=_event_id(item, scheduled_at),
                    scheduled_at=scheduled_at.astimezone(UTC),
                    timing_estimated=True,
                    country=currency,
                    currency=currency,
                    category="Economic calendar",
                    title=title,
                    importance=importance,
                    actual=str(item.get("actual") or "").strip() or None,
                    forecast=str(item.get("forecast") or "").strip() or None,
                    previous=str(item.get("previous") or "").strip() or None,
                    source_updated_at=None,
                    source_url=FOREX_FACTORY_CALENDAR_URL,
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
        return ()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
