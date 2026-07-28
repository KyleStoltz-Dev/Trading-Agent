"""Deterministic HTTP endpoints for adapter reliability tests and local soak runs."""

import json
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import httpx

from app.connectors.metatrader_bridge import MetaTraderReadOnlyBridgeConnector
from app.connectors.oanda import OandaReadOnlyConnector
from app.connectors.trading_economics import TradingEconomicsReadOnlyConnector

FaultKind = Literal["timeout", "reset", "429", "partial-page"]


@dataclass(frozen=True, slots=True)
class SimulatedTradingViewResult:
    status_code: int
    accepted: bool
    duplicate: bool


class DeterministicIntegrationSimulator:
    """One in-memory transport exposing OANDA, MT, news, and webhook endpoints."""

    account_id = "sim-account-001"
    webhook_secret = "simulated-webhook-secret"  # noqa: S105 - inert simulator fixture.

    def __init__(self) -> None:
        self._faults: dict[tuple[str, str], deque[FaultKind]] = defaultdict(deque)
        self.request_counts: dict[tuple[str, str], int] = defaultdict(int)
        self._tradingview_events: set[str] = set()
        self.transport = httpx.MockTransport(self._handle)
        self._clients: list[httpx.AsyncClient] = []

    def inject(
        self,
        provider: str,
        path: str,
        faults: Iterable[FaultKind],
    ) -> None:
        self._faults[(provider, path)].extend(faults)

    def _client(self, provider: str) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=f"https://{provider}.test",
            transport=self.transport,
            timeout=1,
        )
        self._clients.append(client)
        return client

    def oanda(self) -> OandaReadOnlyConnector:
        return OandaReadOnlyConnector(
            token="simulated-token",  # noqa: S106 - inert simulator fixture.
            account_id=self.account_id,
            client=self._client("oanda"),
            stream_client=self._client("oanda-stream"),
        )

    def metatrader(self) -> MetaTraderReadOnlyBridgeConnector:
        return MetaTraderReadOnlyBridgeConnector(
            base_url="https://metatrader.test",
            token="s" * 32,
            account_id=self.account_id,
            platform="mt5",
            poll_interval_seconds=0.1,
            client=self._client("metatrader"),
        )

    def trading_economics(self) -> TradingEconomicsReadOnlyConnector:
        return TradingEconomicsReadOnlyConnector(
            "simulated-key",
            client=self._client("trading-economics"),
        )

    async def tradingview(
        self,
        *,
        event_id: str,
        secret: str,
    ) -> SimulatedTradingViewResult:
        client = self._client("tradingview")
        response = await client.post(
            f"/webhook/{self.account_id}",
            headers={"X-Webhook-Secret": secret},
            json={"event_id": event_id, "symbol": "XAUUSD"},
        )
        payload = response.json()
        return SimulatedTradingViewResult(
            status_code=response.status_code,
            accepted=bool(payload.get("accepted")),
            duplicate=bool(payload.get("duplicate")),
        )

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
        self._clients.clear()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        provider = request.url.host.split(".", 1)[0]
        path = request.url.path
        key = (provider, path)
        self.request_counts[key] += 1
        if queue := self._faults.get(key):
            fault = queue.popleft()
            if fault == "timeout":
                raise httpx.ReadTimeout("simulated timeout", request=request)
            if fault == "reset":
                raise httpx.ReadError("simulated connection reset", request=request)
            if fault == "429":
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0"},
                    json={"error": "simulated rate limit"},
                )
            if fault == "partial-page":
                return httpx.Response(200, content=b'{"truncated":')
        if provider == "oanda":
            return self._oanda(request)
        if provider == "metatrader":
            return self._metatrader(request)
        if provider == "trading-economics":
            return self._trading_economics(request)
        if provider == "tradingview":
            return self._tradingview(request)
        return httpx.Response(404, json={"error": "unknown simulated endpoint"})

    def _oanda(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(
                200,
                json={
                    "lastTransactionID": "1",
                    "account": {
                        "id": self.account_id,
                        "currency": "USD",
                        "balance": "10000",
                        "NAV": "10025",
                        "marginUsed": "100",
                        "marginAvailable": "9925",
                    },
                },
            )
        if path.endswith("/openPositions"):
            return httpx.Response(
                200,
                json={
                    "positions": [
                        {
                            "instrument": "XAU_USD",
                            "long": {"units": "1", "averagePrice": "2400"},
                            "short": {"units": "0"},
                            "unrealizedPL": "25",
                        }
                    ]
                },
            )
        if path.endswith("/pricing"):
            return httpx.Response(
                200,
                json={
                    "prices": [
                        {
                            "instrument": "XAU_USD",
                            "time": "2026-07-27T14:00:00Z",
                            "bids": [{"price": "2424.9"}],
                            "asks": [{"price": "2425.1"}],
                        }
                    ]
                },
            )
        if path.endswith("/candles"):
            return httpx.Response(
                200,
                json={
                    "candles": [
                        {
                            "time": "2026-07-27T13:55:00Z",
                            "mid": {
                                "o": "2424",
                                "h": "2426",
                                "l": "2423",
                                "c": "2425",
                            },
                            "volume": 100,
                            "complete": True,
                        }
                    ]
                },
            )
        if path.endswith("/transactions/idrange"):
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "id": "1",
                            "type": "ORDER_FILL",
                            "time": "2026-07-27T14:00:00Z",
                            "instrument": "XAU_USD",
                            "orderID": "order-1",
                            "units": "1",
                            "price": "2400",
                            "pl": "0",
                            "tradeOpened": {"tradeID": "trade-1", "units": "1"},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": "unknown OANDA endpoint"})

    def _metatrader(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        common = {
            "account_id": self.account_id,
            "market_time": "2026-07-27T14:00:00Z",
        }
        if path == "/v1/health":
            return httpx.Response(
                200,
                json={
                    **common,
                    "read_only": True,
                    "terminal_connected": True,
                    "platform": "mt5",
                },
            )
        if path == "/v1/account":
            return httpx.Response(
                200,
                json={
                    **common,
                    "currency": "USD",
                    "balance": "10000",
                    "equity": "10025",
                    "margin_used": "100",
                    "margin_available": "9925",
                },
            )
        if path == "/v1/positions":
            return httpx.Response(
                200,
                json={
                    **common,
                    "positions": [
                        {
                            "position_id": "position-1",
                            "symbol": "XAUUSD",
                            "net_quantity": "1",
                            "average_price": "2400",
                            "unrealized_pnl": "25",
                            "market_time": common["market_time"],
                        }
                    ],
                },
            )
        if path == "/v1/quote":
            return httpx.Response(
                200,
                json={
                    "symbol": "XAUUSD",
                    "bid": "2424.9",
                    "ask": "2425.1",
                    "market_time": common["market_time"],
                },
            )
        if path == "/v1/candles":
            return httpx.Response(
                200,
                json={
                    "candles": [
                        {
                            "symbol": "XAUUSD",
                            "open": "2424",
                            "high": "2426",
                            "low": "2423",
                            "close": "2425",
                            "volume": "100",
                            "market_time": "2026-07-27T13:55:00Z",
                            "complete": True,
                        }
                    ]
                },
            )
        if path == "/v1/events":
            return httpx.Response(
                200,
                json={
                    **common,
                    "events": [],
                    "next_cursor": "1",
                    "has_more": False,
                    "baseline_only": request.url.params.get("cursor") is None,
                },
            )
        return httpx.Response(404, json={"error": "unknown MetaTrader endpoint"})

    @staticmethod
    def _trading_economics(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/calendar/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "CalendarId": "calendar-1",
                        "Date": "2026-07-27T14:30:00Z",
                        "Country": "United States",
                        "Currency": "USD",
                        "Category": "Inflation",
                        "Event": "Core PCE",
                        "Importance": 3,
                        "DateSpan": "0",
                    }
                ],
            )
        if request.url.path == "/news":
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": "news-1",
                        "Title": "Simulated central-bank headline",
                        "Description": "Deterministic integration fixture.",
                        "Date": "2026-07-27T14:00:00Z",
                        "Country": "United States",
                        "Importance": 2,
                    }
                ],
            )
        return httpx.Response(404, json={"error": "unknown news endpoint"})

    def _tradingview(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Webhook-Secret") != self.webhook_secret:
            return httpx.Response(401, json={"accepted": False, "duplicate": False})
        event_id = str(json.loads(request.content)["event_id"])
        duplicate = event_id in self._tradingview_events
        self._tradingview_events.add(event_id)
        return httpx.Response(
            200 if duplicate else 202,
            json={"accepted": True, "duplicate": duplicate},
        )
