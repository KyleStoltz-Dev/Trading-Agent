import asyncio
from datetime import date

import httpx

from app.config import Settings
from app.connectors.oanda import OandaReadOnlyConnector
from app.connectors.trading_economics import TradingEconomicsReadOnlyConnector


def test_oanda_read_connector_normalizes_account_positions_and_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
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
            return httpx.Response(
                200,
                json={
                    "lastTransactionID": "10",
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
        if path.endswith("/sinceid"):
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
        initial_events, initial_cursor = await connector.events_since(None)
        events, cursor = await connector.events_since(initial_cursor)
        await client.aclose()
        return quote, candles, account, positions, initial_events, events, cursor

    quote, candles, account, positions, initial_events, events, cursor = asyncio.run(
        exercise()
    )
    assert quote.instrument == "XAU_USD"
    assert candles[0].complete is True
    assert account.external_account_id == "account-1"
    assert positions[0].net_quantity == 2
    assert initial_events == ()
    assert events[0].external_trade_id == "trade-1"
    assert cursor == "11"


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
