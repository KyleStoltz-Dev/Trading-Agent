import asyncio
import uuid
from datetime import date

import httpx
import pytest

from app.config import Settings
from app.connectors.factory import BrokerConfigurationError, create_broker_connector
from app.connectors.forex_factory import (
    ForexFactoryError,
    ForexFactoryReadOnlyConnector,
)
from app.connectors.oanda import (
    OandaConnectorError,
    OandaReadOnlyConnector,
)
from app.connectors.oanda import (
    _retry_delay as oanda_retry_delay,
)
from app.connectors.trading_economics import TradingEconomicsReadOnlyConnector
from app.models import BrokerConnection, TradingAccount


def test_oanda_read_connector_normalizes_account_positions_and_events() -> None:
    summary_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal summary_calls
        path = request.url.path
        if path.endswith("/pricing"):
            return httpx.Response(
                200,
                json={
                    "prices": [
                        {
                            "type": "PRICE",
                            "instrument": "XAU_USD",
                            "time": "2026-07-23T15:00:00Z",
                            "bids": [{"price": "2399.5"}],
                            "asks": [{"price": "2400"}],
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
                            "time": "2026-07-23T14:55:00Z",
                            "mid": {
                                "o": "2398",
                                "h": "2401",
                                "l": "2397",
                                "c": "2400",
                            },
                            "volume": 10,
                            "complete": True,
                        }
                    ]
                },
            )
        if path.endswith("/summary"):
            summary_calls += 1
            return httpx.Response(
                200,
                json={
                    "lastTransactionID": "10" if summary_calls <= 2 else "11",
                    "account": {
                        "id": "account-1",
                        "currency": "USD",
                        "balance": "10000",
                        "NAV": "10020",
                        "marginUsed": "100",
                        "marginAvailable": "9920",
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
                            "long": {"units": "2", "averagePrice": "2400"},
                            "short": {"units": "0"},
                            "unrealizedPL": "20",
                        }
                    ]
                },
            )
        if path.endswith("/idrange"):
            assert request.url.params["from"] == "11"
            assert request.url.params["to"] == "11"
            return httpx.Response(
                200,
                json={
                    "lastTransactionID": "11",
                    "transactions": [
                        {
                            "id": "11",
                            "type": "ORDER_FILL",
                            "time": "2026-07-23T15:00:00Z",
                            "instrument": "XAU_USD",
                            "units": "2",
                            "price": "2400",
                            "pl": "0",
                            "tradeOpened": {"tradeID": "trade-1"},
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {path}")

    async def exercise():
        client = httpx.AsyncClient(
            base_url="https://api-fxpractice.oanda.com",
            transport=httpx.MockTransport(handler),
        )
        connector = OandaReadOnlyConnector(
            token="secret-token",
            account_id="account-1",
            client=client,
            stream_client=client,
        )
        quote = await connector.latest_quote("XAU_USD")
        candles = await connector.candles("XAU_USD", "M5", count=1)
        account = await connector.account()
        positions = await connector.positions()
        initial_page = await connector.events_since(None)
        page = await connector.events_since(initial_page.cursor_after)
        await client.aclose()
        return quote, candles, account, positions, initial_page, page

    quote, candles, account, positions, initial_page, page = asyncio.run(
        exercise()
    )
    assert quote.instrument == "XAU_USD"
    assert candles[0].complete is True
    assert account.external_account_id == "account-1"
    assert positions[0].net_quantity == 2
    assert initial_page.events == ()
    assert initial_page.cursor_before is None
    assert initial_page.cursor_after == "10"
    assert initial_page.has_more is False
    assert initial_page.coverage == "baseline"
    assert page.events[0].external_trade_id == "trade-1"
    assert page.cursor_before == "10"
    assert page.cursor_after == "11"
    assert page.has_more is False
    assert page.coverage == "incremental"


def test_oanda_response_body_is_bounded() -> None:
    async def exercise() -> None:
        client = httpx.AsyncClient(
            base_url="https://api-fxpractice.oanda.com",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"x" * 101)
            ),
        )
        connector = OandaReadOnlyConnector(
            token="secret-token",
            account_id="account-1",
            client=client,
            stream_client=client,
            maximum_response_bytes=100,
        )
        try:
            with pytest.raises(OandaConnectorError, match="exceeded"):
                await connector.latest_quote("XAU_USD")
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_oanda_history_uses_bounded_transaction_id_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/summary"):
            return httpx.Response(200, json={"lastTransactionID": "2005"})
        if request.url.path.endswith("/idrange"):
            assert request.url.params["from"] == "1"
            assert request.url.params["to"] == "1000"
            return httpx.Response(200, json={"transactions": []})
        raise AssertionError(request.url.path)

    async def exercise():
        client = httpx.AsyncClient(
            base_url="https://api-fxpractice.oanda.com",
            transport=httpx.MockTransport(handler),
        )
        connector = OandaReadOnlyConnector(
            token="secret-token",
            account_id="account-1",
            client=client,
            stream_client=client,
        )
        try:
            return await connector.events_since("0")
        finally:
            await client.aclose()

    page = asyncio.run(exercise())
    assert page.cursor_after == "1000"
    assert page.has_more is True
    assert page.coverage == "incremental"


def test_oanda_invalid_retry_after_falls_back_without_crashing() -> None:
    assert oanda_retry_delay("not-a-delay", 1) == 0.5


def test_trading_economics_connector_preserves_provider_timestamps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/calendar/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "CalendarId": "event-1",
                        "Date": "2026-07-24T12:30:00",
                        "DateSpan": "0",
                        "Country": "United States",
                        "Currency": "USD",
                        "Category": "Inflation",
                        "Event": "Core PCE",
                        "Importance": 3,
                        "Forecast": "0.2%",
                        "Previous": "0.1%",
                        "LastUpdate": "2026-07-23T12:00:00",
                    }
                ],
            )
        if request.url.path == "/news":
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": "news-1",
                        "Title": "Fed decision approaches",
                        "Description": "A provider summary.",
                        "Date": "2026-07-23T15:00:00",
                        "Country": "United States",
                        "Category": "Interest Rate",
                        "Symbol": "FDTR",
                        "Importance": 3,
                        "Url": "/united-states/interest-rate",
                    }
                ],
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async def exercise():
        client = httpx.AsyncClient(
            base_url="https://api.tradingeconomics.com",
            transport=httpx.MockTransport(handler),
        )
        connector = TradingEconomicsReadOnlyConnector("secret", client=client)
        calendar = await connector.calendar(
            start=date(2026, 7, 23),
            end=date(2026, 7, 24),
            countries=["United States"],
            minimum_importance=3,
        )
        news = await connector.news(limit=10)
        await client.aclose()
        return calendar, news

    calendar, news = asyncio.run(exercise())
    assert calendar[0].scheduled_at.utcoffset().total_seconds() == 0
    assert calendar[0].importance == 3
    assert news[0].published_at.utcoffset().total_seconds() == 0
    assert news[0].source_url.startswith("https://tradingeconomics.com/")


def test_forex_factory_connector_filters_and_normalizes_weekly_events() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "title": "Federal Funds Rate",
                    "country": "USD",
                    "date": "2026-07-29T14:00:00-04:00",
                    "impact": "High",
                    "forecast": "3.75%",
                    "previous": "3.75%",
                },
                {
                    "title": "Crude Oil Inventories",
                    "country": "USD",
                    "date": "2026-07-29T10:30:00-04:00",
                    "impact": "Low",
                    "forecast": "0.7M",
                    "previous": "2.0M",
                },
                {
                    "title": "CPI",
                    "country": "AUD",
                    "date": "2026-07-29T21:30:00-04:00",
                    "impact": "High",
                    "forecast": "0.2%",
                    "previous": "-0.7%",
                },
            ],
        )

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        connector = ForexFactoryReadOnlyConnector(client=client)
        try:
            events = await connector.calendar(
                start=date(2026, 7, 29),
                end=date(2026, 7, 29),
                countries=("United States",),
                minimum_importance=2,
            )
            headlines = await connector.news(limit=5)
            return events, headlines
        finally:
            await client.aclose()

    events, headlines = asyncio.run(exercise())

    assert len(events) == 1
    assert events[0].title == "Federal Funds Rate"
    assert events[0].currency == "USD"
    assert events[0].country == "USD"
    assert events[0].importance == 3
    assert events[0].scheduled_at.isoformat() == "2026-07-29T18:00:00+00:00"
    assert len(events[0].external_id) == 64
    assert headlines == ()


def test_forex_factory_rate_limit_has_customer_safe_error() -> None:
    async def exercise() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    429,
                    headers={"Retry-After": "0"},
                )
            )
        )
        connector = ForexFactoryReadOnlyConnector(client=client)
        try:
            with pytest.raises(ForexFactoryError, match="temporarily unavailable"):
                await connector.calendar(
                    start=date(2026, 7, 29),
                    end=date(2026, 7, 29),
                    countries=(),
                )
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_settings_repr_masks_all_credentials() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:secret-db@localhost/db",
        openai_api_key="secret-openai",
        anthropic_api_key="secret-anthropic",
        oanda_api_token="secret-oanda",
        oanda_account_id="secret-account",
        trading_agent_api_key="secret-local-api",
        trading_economics_api_key="secret-news",
    )
    rendered = repr(settings)

    for secret in (
        "secret-db",
        "secret-openai",
        "secret-anthropic",
        "secret-oanda",
        "secret-account",
        "secret-local-api",
        "secret-news",
    ):
        assert secret not in rendered


def test_broker_factory_rejects_selected_account_without_secret_reference() -> None:
    account = TradingAccount(
        workspace_id=uuid.uuid4(),
        broker="OANDA",
        external_account_id="selected-account",
        label="Selected",
        currency="USD",
        mode="practice",
        active=True,
    )
    account.id = uuid.uuid4()
    connection = BrokerConnection(
        workspace_id=account.workspace_id,
        account_id=account.id,
        provider="oanda-v20",
        environment="practice",
    )

    with pytest.raises(BrokerConfigurationError, match="secret reference"):
        create_broker_connector(
            Settings(
                broker_provider="oanda",
                oanda_api_token="token",
                oanda_account_id="different-account",
            ),
            account=account,
            connection=connection,
        )
