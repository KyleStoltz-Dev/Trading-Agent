from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.metatrader_bridge_server import MetaTrader5Terminal, create_bridge_app, run


class FakeMT5:
    TIMEFRAME_M5 = 5
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def initialize(self, *_args):
        return True

    def shutdown(self):
        return None

    def last_error(self):
        return (0, "ok")

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def account_info(self):
        return SimpleNamespace(
            login=123456,
            currency="USD",
            balance=10000,
            equity=10025,
            margin=100,
            margin_free=9925,
        )

    def symbol_info_tick(self, _instrument):
        return SimpleNamespace(
            bid=2399.5,
            ask=2400,
            time_msc=1785081600000,
        )

    def copy_rates_from_pos(self, _instrument, _timeframe, _start, _count):
        return [
            {
                "time": 1785081300,
                "open": 2398,
                "high": 2401,
                "low": 2397,
                "close": 2400,
                "tick_volume": 81,
                "real_volume": 0,
            }
        ]

    def positions_get(self):
        return [
            {
                "ticket": 7001,
                "symbol": "XAUUSD",
                "type": self.POSITION_TYPE_BUY,
                "volume": 1,
                "price_open": 2399,
                "profit": 30,
                "time": 1785080000,
                "time_msc": 1785080000000,
            },
            {
                "ticket": 7002,
                "symbol": "XAUUSD",
                "type": self.POSITION_TYPE_SELL,
                "volume": 0.25,
                "price_open": 2401,
                "profit": -5,
                "time": 1785080100,
                "time_msc": 1785080100000,
            },
        ]

    def history_deals_get(self, _start=None, _end=None, *, position=None):
        deals = [
            {
                "ticket": 9001,
                "order": 8001,
                "time": 1785081600,
                "time_msc": 1785081600000,
                "type": self.DEAL_TYPE_SELL,
                "entry": self.DEAL_ENTRY_OUT,
                "position_id": 7001,
                "volume": 0.25,
                "price": 2400,
                "commission": -1,
                "fee": -0.1,
                "swap": -0.25,
                "profit": 50,
                "symbol": "XAUUSD",
            }
        ]
        if position is not None:
            return [item for item in deals if int(item["position_id"]) == int(position)]
        return deals


def _client() -> TestClient:
    terminal = MetaTrader5Terminal(FakeMT5(), account_id="123456")
    terminal.connect()
    return TestClient(create_bridge_app(terminal, token="x" * 32))


def test_bridge_server_requires_bearer_auth_and_exposes_only_read_routes() -> None:
    with _client() as client:
        assert client.get("/v1/health").status_code == 401
        response = client.get(
            "/v1/health",
            headers={"Authorization": f"Bearer {'x' * 32}"},
        )
        assert response.status_code == 200
        assert response.json()["read_only"] is True
        assert client.get("/openapi.json").status_code == 404
        paths = {route.path for route in client.app.routes}
        assert paths == {
            "/v1/health",
            "/v1/quote",
            "/v1/candles",
            "/v1/account",
            "/v1/positions",
            "/v1/events",
        }


def test_bridge_server_normalizes_net_positions_and_bounded_history_cursor() -> None:
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    with _client() as client:
        positions = client.get("/v1/positions", headers=headers).json()["positions"]
        assert positions[0]["symbol"] == "XAUUSD"
        assert positions[0]["net_quantity"] == "0.75"
        assert positions[0]["average_price"] is None
        assert positions[0]["unrealized_pnl"] == "25"

        baseline = client.get("/v1/events", headers=headers).json()
        assert baseline["events"] == []
        assert baseline["baseline_only"] is True

        history = client.get(
            "/v1/events",
            params={"cursor": "2026-01-01T00:00:00Z"},
            headers=headers,
        ).json()
        event = history["events"][0]
        assert event["event_id"] == "9001"
        assert event["quantity"] == "-0.25"
        assert event["commission"] == "-1.1"
        assert event["trade_id"] == "7001"
        assert event["infer_trade_open"] is False
        assert event["trade_effects"] == [
            {
                "external_trade_id": "7001",
                "effect": "reduced",
                "quantity": "-0.25",
                "realized_pnl": "50",
            }
        ]


def test_paginated_history_marks_only_the_true_final_exit_closed(monkeypatch) -> None:
    monkeypatch.setattr("app.metatrader_bridge_server._MAX_HISTORY_EVENTS", 2)

    class PaginatedMT5(FakeMT5):
        def positions_get(self):
            return []

        def history_deals_get(self, _start=None, _end=None, *, position=None):
            deals = [
                {
                    "ticket": 1,
                    "order": 11,
                    "time": 1,
                    "time_msc": 1_000,
                    "type": self.DEAL_TYPE_BUY,
                    "entry": self.DEAL_ENTRY_OUT,
                    "position_id": 101,
                    "volume": 1,
                    "price": 1,
                    "profit": 1,
                    "symbol": "X",
                },
                {
                    "ticket": 2,
                    "order": 12,
                    "time": 2,
                    "time_msc": 2_000,
                    "type": self.DEAL_TYPE_BUY,
                    "entry": self.DEAL_ENTRY_IN,
                    "position_id": 202,
                    "volume": 1,
                    "price": 1,
                    "profit": 0,
                    "symbol": "X",
                },
                {
                    "ticket": 3,
                    "order": 13,
                    "time": 3,
                    "time_msc": 3_000,
                    "type": self.DEAL_TYPE_SELL,
                    "entry": self.DEAL_ENTRY_OUT,
                    "position_id": 202,
                    "volume": 1,
                    "price": 1,
                    "profit": 1,
                    "symbol": "X",
                },
            ]
            if position is not None:
                return [
                    item for item in deals if int(item["position_id"]) == int(position)
                ]
            return deals

    terminal = MetaTrader5Terminal(PaginatedMT5(), account_id="123456")
    first = terminal.events("1970-01-01T00:00:00Z")
    second = terminal.events(first["next_cursor"])

    assert first["has_more"] is True
    assert first["events"][0]["trade_effects"][0]["effect"] == "closed"
    assert second["events"][0]["trade_effects"][0]["effect"] == "closed"


def test_bridge_refuses_plaintext_non_loopback_binding(monkeypatch) -> None:
    monkeypatch.setenv("METATRADER_BRIDGE_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("METATRADER_BRIDGE_ALLOW_NETWORK", "true")
    monkeypatch.delenv("METATRADER_BRIDGE_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("METATRADER_BRIDGE_TLS_KEYFILE", raising=False)
    monkeypatch.setattr(
        "app.metatrader_bridge_server.get_settings",
        lambda: Settings(
            metatrader_bridge_token="x" * 32,
            metatrader_account_id="123456",
        ),
    )

    with pytest.raises(RuntimeError, match="requires direct TLS"):
        run()
