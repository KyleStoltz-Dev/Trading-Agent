import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.connectors.factory import BrokerConfigurationError
from app.connectors.kraken import KrakenConnectorError
from app.market_data.contracts import AccountState, Candle, PositionState, Quote
from app.services.workspaces import RequestScope


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


class _TestBrokerConnector:
    name = "oanda"

    async def latest_quote(self, instrument: str) -> Quote:
        timestamp = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
        return Quote(
            instrument=instrument,
            bid=Decimal("2399.1"),
            ask=Decimal("2400.1"),
            market_time=timestamp,
            retrieved_at=timestamp,
            source="oanda",
            venue="OANDA",
        )

    async def candles(self, instrument: str, timeframe: str, *, count: int):
        timestamp = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
        candles = (
            Candle(
                instrument=instrument,
                timeframe=timeframe,
                started_at=timestamp,
                open=Decimal("2390"),
                high=Decimal("2410"),
                low=Decimal("2380"),
                close=Decimal("2395"),
                volume=Decimal("100"),
                complete=True,
                retrieved_at=timestamp,
                source="oanda",
                venue="OANDA",
            ),
        )
        return candles[:count]

    async def account(self) -> AccountState:
        timestamp = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
        return AccountState(
            external_account_id="101-001-123",
            currency="USD",
            balance=Decimal("150500"),
            equity=Decimal("150742.80"),
            margin_used=Decimal("1250"),
            margin_available=Decimal("149492.80"),
            market_time=timestamp,
            retrieved_at=timestamp,
            source="oanda",
        )

    async def positions(self) -> tuple[PositionState, ...]:
        timestamp = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
        return (
            PositionState(
                external_id="XAU_USD",
                instrument="XAU_USD",
                net_quantity=Decimal("1.5"),
                average_price=Decimal("2481.3"),
                unrealized_pnl=Decimal("242.8"),
                market_time=timestamp,
                retrieved_at=timestamp,
                source="oanda",
            ),
        )

    async def aclose(self) -> None:
        return None


def _api_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://ignored:ignored@localhost/ignored",
        database_auto_migrate=False,
        trading_agent_api_key="x" * 32,
        broker_provider="oanda",
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


def test_broker_state_endpoint_returns_account_and_positions(monkeypatch) -> None:
    settings = _api_settings()
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    account = SimpleNamespace(
        id=scope.account_id,
        workspace_id=scope.workspace_id,
        broker="OANDA",
        active=True,
    )
    connection = SimpleNamespace(
        account_id=scope.account_id,
        workspace_id=scope.workspace_id,
        provider="oanda-v20",
        environment="practice",
        status="healthy",
    )
    database = Mock()
    database.scalar.side_effect = (account, connection)
    captured = {}
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    def create_connector(_settings, *, account, connection):
        captured["account"] = account
        captured["connection"] = connection
        return _TestBrokerConnector()

    monkeypatch.setattr(main_module, "create_broker_connector", create_connector)
    main_module.app.dependency_overrides[main_module.get_db] = lambda: database
    main_module.app.dependency_overrides[main_module.require_request_scope] = lambda: scope

    try:
        with TestClient(main_module.app) as client:
            response = client.get(
                "/api/broker-state",
                headers={"X-API-Key": "x" * 32},
            )
    finally:
        main_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "oanda"
    assert payload["balance"] == "150500"
    assert payload["equity"] == "150742.80"
    assert payload["positions"][0]["instrument"] == "XAU_USD"
    assert captured == {"account": account, "connection": connection}


def test_broker_state_requires_an_explicit_account_scope(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: _api_settings())

    with TestClient(main_module.app) as client:
        response = client.get(
            "/api/broker-state",
            headers={"X-API-Key": "x" * 32},
        )

    assert response.status_code == 428
    assert "X-Workspace-ID and X-Account-ID" in response.json()["detail"]


def test_oanda_market_data_uses_scoped_saved_broker_credentials(monkeypatch) -> None:
    settings = _api_settings()
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    account = SimpleNamespace(
        id=scope.account_id,
        workspace_id=scope.workspace_id,
        broker="OANDA",
        active=True,
    )
    connection = SimpleNamespace(
        account_id=scope.account_id,
        workspace_id=scope.workspace_id,
        provider="oanda-v20",
        environment="practice",
        status="healthy",
    )
    database = Mock()
    database.scalar.return_value = connection
    captured = {}
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "validate_scope", lambda _db, _scope: account)

    def create_connector(_settings, *, account, connection):
        captured["account"] = account
        captured["connection"] = connection
        return _TestBrokerConnector()

    monkeypatch.setattr(main_module, "create_broker_connector", create_connector)
    main_module.app.dependency_overrides[main_module.get_db] = lambda: database

    try:
        with TestClient(main_module.app) as client:
            response = client.get(
                "/api/market-data",
                params={"provider": "oanda", "instrument": "XAU_USD"},
                headers={
                    "X-API-Key": "x" * 32,
                    "X-Workspace-ID": str(scope.workspace_id),
                    "X-Account-ID": str(scope.account_id),
                },
            )
    finally:
        main_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["provider"] == "oanda"
    assert captured == {"account": account, "connection": connection}


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
