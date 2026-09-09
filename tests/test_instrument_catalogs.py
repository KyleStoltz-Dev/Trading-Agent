import asyncio

import httpx

from app.connectors.alpaca import AlpacaReadOnlyConnector
from app.connectors.kraken import KrakenReadOnlyConnector
from app.connectors.metatrader_bridge import MetaTraderReadOnlyBridgeConnector
from app.connectors.oanda import OandaReadOnlyConnector


def test_oanda_catalog_uses_account_tradeable_instruments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/accounts/account-1/instruments"
        return httpx.Response(
            200,
            json={
                "instruments": [
                    {
                        "name": "XAU_USD",
                        "displayName": "Gold/USD",
                        "type": "METAL",
                    },
                    {
                        "name": "EUR_USD",
                        "displayName": "EUR/USD",
                        "type": "CURRENCY",
                    },
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api-fxpractice.oanda.com",
    )
    connector = OandaReadOnlyConnector(
        token="token",
        account_id="account-1",
        client=client,
        stream_client=client,
    )
    instruments = asyncio.run(connector.instruments())

    assert [item.symbol for item in instruments] == ["XAU_USD", "EUR_USD"]
    assert instruments[0].display_name == "Gold/USD"
    asyncio.run(client.aclose())


def test_kraken_catalog_uses_every_public_tradable_pair() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/0/public/AssetPairs"
        assert request.url.params["assetVersion"] == "1"
        return httpx.Response(
            200,
            json={
                "error": [],
                "result": {
                    "BTC/USD": {
                        "wsname": "BTC/USD",
                        "aclass_base": "currency",
                        "status": "online",
                    },
                    "ETH/EUR": {
                        "wsname": "ETH/EUR",
                        "aclass_base": "currency",
                        "status": "online",
                    },
                },
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.kraken.com",
    )
    connector = KrakenReadOnlyConnector(client=client)
    instruments = asyncio.run(connector.instruments())

    assert [item.symbol for item in instruments] == ["BTC_USD", "ETH_EUR"]
    asyncio.run(client.aclose())


def test_alpaca_catalog_uses_authenticated_active_equities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://paper-api.alpaca.markets/v2/assets"
            "?status=active&asset_class=us_equity"
        )
        assert request.headers["APCA-API-KEY-ID"] == "key"
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "class": "us_equity",
                    "exchange": "NASDAQ",
                    "tradable": True,
                },
                {
                    "symbol": "BRK.B",
                    "name": "Berkshire Hathaway Inc.",
                    "class": "us_equity",
                    "exchange": "NYSE",
                    "tradable": True,
                },
            ],
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://data.alpaca.markets/v2",
        headers={
            "APCA-API-KEY-ID": "key",
            "APCA-API-SECRET-KEY": "secret",
        },
    )
    connector = AlpacaReadOnlyConnector(
        key_id="key",
        secret_key="secret",
        base_url="https://data.alpaca.markets/v2",
        client=client,
    )
    instruments = asyncio.run(connector.instruments())

    assert [item.symbol for item in instruments] == ["AAPL", "BRK.B"]
    assert instruments[1].venue == "NYSE"
    asyncio.run(client.aclose())


def test_metatrader_catalog_preserves_terminal_symbols_and_suffixes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/symbols"
        return httpx.Response(
            200,
            json={
                "account_id": "123456",
                "symbols": [
                    {
                        "symbol": "XAUUSD.a",
                        "description": "Gold vs US Dollar",
                        "path": "Forex\\Metals",
                        "tradable": True,
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bridge.example",
    )
    connector = MetaTraderReadOnlyBridgeConnector(
        base_url="https://bridge.example",
        token="t" * 32,
        account_id="123456",
        platform="mt5",
        client=client,
    )
    instruments = asyncio.run(connector.instruments())

    assert instruments[0].symbol == "XAUUSD.a"
    assert instruments[0].display_name == "Gold vs US Dollar"
    asyncio.run(client.aclose())
