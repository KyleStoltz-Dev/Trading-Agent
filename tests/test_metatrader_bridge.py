import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.connectors.factory import BrokerConfigurationError, create_metatrader_connector
from app.connectors.metatrader_bridge import (
    MetaTraderBridgeError,
    MetaTraderReadOnlyBridgeConnector,
    _retry_delay,
)

NOW = datetime(2026, 7, 26, 15, 30, tzinfo=UTC)


def _connector(handler, **changes) -> MetaTraderReadOnlyBridgeConnector:
    token = "t" * 32
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bridge.example",
        headers={"Authorization": f"Bearer {token}"},
    )
    values = {
        "base_url": "https://bridge.example",
        "token": token,
        "account_id": "123456",
        "platform": "mt5",
        "client": client,
    }
    values.update(changes)
    return MetaTraderReadOnlyBridgeConnector(**values)


def test_bridge_rejects_insecure_remote_http_and_accepts_loopback() -> None:
    with pytest.raises(ValueError, match="require HTTPS"):
        MetaTraderReadOnlyBridgeConnector(
            base_url="http://bridge.example",
            token="t" * 32,
            account_id="123456",
            platform="mt5",
        )

    connector = MetaTraderReadOnlyBridgeConnector(
        base_url="http://127.0.0.1:8765",
        token="t" * 32,
        account_id="123456",
        platform="mt5",
    )
    asyncio.run(connector.aclose())


def test_bridge_rejects_nonpositive_response_limit() -> None:
    with pytest.raises(ValueError, match="response size"):
        MetaTraderReadOnlyBridgeConnector(
            base_url="https://bridge.example",
            token="t" * 32,
            account_id="123456",
            platform="mt5",
            maximum_response_bytes=0,
        )


def test_factory_rejects_short_bridge_token_before_network_use() -> None:
    settings = Settings(
        broker_provider="metatrader",
        metatrader_bridge_token="short",
        metatrader_account_id="123456",
    )
    with pytest.raises(BrokerConfigurationError, match="at least 32"):
        create_metatrader_connector(settings)


def test_bridge_health_requires_read_only_attestation_and_matching_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {'t' * 32}"
        return httpx.Response(
            200,
            json={
                "platform": "mt5",
                "account_id": "123456",
                "read_only": True,
                "terminal_connected": True,
            },
        )

    connector = _connector(handler)
    health = asyncio.run(connector.health())
    assert health["terminal_connected"] is True
    asyncio.run(connector.aclose())


def test_bridge_health_rejects_a_disconnected_terminal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "platform": "mt5",
                "account_id": "123456",
                "read_only": True,
                "terminal_connected": False,
            },
        )

    connector = _connector(handler)
    with pytest.raises(MetaTraderBridgeError, match="not connected"):
        asyncio.run(connector.health())
    asyncio.run(connector.aclose())


def test_bridge_normalizes_market_account_position_and_execution_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/quote":
            return httpx.Response(
                200,
                json={
                    "symbol": "XAUUSD",
                    "bid": "2399.50",
                    "ask": "2400.00",
                    "time": NOW.isoformat(),
                },
            )
        if request.url.path == "/v1/candles":
            return httpx.Response(
                200,
                json={
                    "candles": [
                        {
                            "time": NOW.isoformat(),
                            "open": "2398",
                            "high": "2401",
                            "low": "2397",
                            "close": "2400",
                            "volume": "81",
                            "complete": True,
                        }
                    ]
                },
            )
        if request.url.path == "/v1/account":
            return httpx.Response(
                200,
                json={
                    "account_id": "123456",
                    "currency": "USD",
                    "balance": "10000",
                    "equity": "10025",
                    "margin_used": "100",
                    "margin_available": "9925",
                    "time": NOW.isoformat(),
                },
            )
        if request.url.path == "/v1/positions":
            return httpx.Response(
                200,
                json={
                    "account_id": "123456",
                    "positions": [
                        {
                            "position_id": "7001",
                            "symbol": "XAUUSD",
                            "net_quantity": "-0.25",
                            "average_price": "2400",
                            "unrealized_pnl": "25",
                            "time": NOW.isoformat(),
                        }
                    ],
                },
            )
        if request.url.path == "/v1/events":
            return httpx.Response(
                200,
                json={
                    "account_id": "123456",
                    "next_cursor": "1722007800000:9001",
                    "has_more": True,
                    "events": [
                        {
                            "event_id": "9001",
                            "event_type": "DEAL_FILL",
                            "time_msc": int(NOW.timestamp() * 1_000),
                            "symbol": "XAUUSD",
                            "order_id": "8001",
                            "trade_id": "7001",
                            "quantity": "-0.25",
                            "price": "2400",
                            "realized_pnl": "50",
                            "commission": "-1",
                            "financing": "-0.25",
                            "trade_effects": [
                                {
                                    "external_trade_id": "7001",
                                    "effect": "closed",
                                    "quantity": "0.25",
                                    "realized_pnl": "50",
                                }
                            ],
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    connector = _connector(handler)

    async def read_all():
        return (
            await connector.latest_quote("XAUUSD"),
            await connector.candles("XAUUSD", "M5", count=1),
            await connector.account(),
            await connector.positions(),
            await connector.events_since("2026-01-01T00:00:00Z"),
        )

    quote, candles, account, positions, page = asyncio.run(read_all())
    assert quote.midpoint == Decimal("2399.75")
    assert candles[0].close == Decimal("2400")
    assert account.external_account_id == "123456"
    assert positions[0].net_quantity == Decimal("-0.25")
    assert page.events[0].external_id == "9001"
    assert page.events[0].commission == Decimal("-1")
    assert page.events[0].trade_effects[0].effect == "closed"
    assert page.events[0].infer_trade_open is False
    assert page.cursor_before == "2026-01-01T00:00:00Z"
    assert page.cursor_after == "1722007800000:9001"
    assert page.has_more is True
    assert page.coverage == "incremental"
    asyncio.run(connector.aclose())


def test_bridge_bounds_response_size_and_does_not_echo_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"secret":"' + (b"x" * 300) + b'"}')

    connector = _connector(handler, maximum_response_bytes=100)
    with pytest.raises(MetaTraderBridgeError, match="exceeded"):
        asyncio.run(connector.health())
    asyncio.run(connector.aclose())


def test_invalid_retry_after_falls_back_without_crashing() -> None:
    assert _retry_delay("not-a-delay", 2) == 1.0
