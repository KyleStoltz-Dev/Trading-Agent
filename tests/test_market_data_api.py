from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.connectors.factory import BrokerConfigurationError
from app.connectors.kraken import KrakenConnectorError
from app.market_data.contracts import Candle, Quote


class _TestConnector:
    def __init__(self, *, quote: Quote, candles: tuple[Candle, ...]):
        self.quote = quote
        self._candles = candles
        self.name = "kraken"
        self.venue = "KRAKEN"

    async def latest_quote(self, _instrument: str) -> Quote:
        return self.quote

    async def candles(self, _instrument: str, _timeframe: str, *, count: int):
        return self._candles[:count]

    async def aclose(self) -> None:
        return None


def _api_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://ignored:ignored@localhost/ignored",
        database_auto_migrate=False,
        trading_agent_api_key="x" * 32,
    )


def test_market_data_endpoint_returns_normalized_payload(monkeypatch) -> None:
    settings = _api_settings()
    quote = Quote(
        instrument="XAU_USD",
        bid=Decimal("2399.1"),
        ask=Decimal("2400.1"),
        market_time=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        retrieved_at=datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC),
        source="kraken",
        venue="KRAKEN",
    )
    candles = (
        Candle(
            instrument="XAU_USD",
            timeframe="H4",
            started_at=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
            open=Decimal("2390"),
            high=Decimal("2410"),
            low=Decimal("2380"),
            close=Decimal("2395"),
            volume=Decimal("100"),
            complete=True,
            retrieved_at=datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC),
            source="kraken",
            venue="KRAKEN",
        ),
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module,
        "create_market_data_connector",
        lambda _settings, _provider: _TestConnector(quote=quote, candles=candles),
    )

    with TestClient(main_module.app) as client:
        response = client.get(
            "/api/market-data",
            params={"provider": "kraken", "instrument": "XAUUSD", "timeframe": "H4", "count": 2},
            headers={"X-API-Key": "x" * 32},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "kraken"
    assert payload["instrument"] == "XAUUSD"
    assert payload["quote"]["instrument"] == "XAU_USD"
    assert payload["candles"][0]["source"] == "kraken"


def test_market_data_endpoint_maps_invalid_provider_to_400(monkeypatch) -> None:
    settings = _api_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    def fail(_settings: Settings, _provider: str):
        raise BrokerConfigurationError("market data provider is unknown or unsupported: nope")

    monkeypatch.setattr(main_module, "create_market_data_connector", fail)

    with TestClient(main_module.app) as client:
        response = client.get(
            "/api/market-data",
            params={"provider": "nope"},
            headers={"X-API-Key": "x" * 32},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "market data provider is unknown or unsupported: nope"


def test_market_data_endpoint_maps_connector_error_to_503(monkeypatch) -> None:
    settings = _api_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    async def bad_quote(_instrument: str):
        raise KrakenConnectorError("temporary failure")

    class _BadConnector:
        name = "kraken"
        venue = "KRAKEN"

        async def latest_quote(self, instrument: str) -> None:
            await bad_quote(instrument)

        async def candles(self, _instrument: str, _timeframe: str, *, count: int):
            raise AssertionError("should not call candles when quote failed")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        main_module,
        "create_market_data_connector",
        lambda _settings, _provider: _BadConnector(),
    )

    with TestClient(main_module.app) as client:
        response = client.get(
            "/api/market-data",
            params={"provider": "kraken"},
            headers={"X-API-Key": "x" * 32},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "temporary failure"
