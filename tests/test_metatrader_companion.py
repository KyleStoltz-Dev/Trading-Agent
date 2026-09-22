import copy
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.metatrader_companion import MAX_BODY_BYTES, create_companion_app, run

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
TOKEN = "synthetic-companion-token-for-tests-only"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ROUTE = "/v1/companion/snapshot"
HEALTH = "/v1/companion/health"


def sample():
    end = int(NOW.timestamp())
    return {
        "schema_version": 1,
        "platform": "mt5",
        "read_only": True,
        "terminal_connected": True,
        "account_id": "123456",
        "broker_server": "Synthetic-Demo",
        "sequence": 1,
        "captured_at": NOW.isoformat(),
        "market_time_basis": "broker_server_unconverted",
        "account": {
            "currency": "USD",
            "balance": "10000",
            "equity": "10001",
            "margin_used": "5",
            "margin_available": "9996",
        },
        "quote": {
            "symbol": "XAUUSD.a",
            "bid": "2399.1234567890",
            "ask": "2400",
            "broker_time_msc": end * 1000,
        },
        "history": {
            "coverage": "recent_window",
            "broker_from_seconds": end - 7 * 86400,
            "broker_to_seconds": end,
            "total_deals": 1,
            "truncated": False,
            "deals": [
                {
                    "ticket": "8000",
                    "order_ticket": "7000",
                    "position_id": "6000",
                    "symbol": "XAUUSD.a",
                    "deal_type": 0,
                    "entry_type": 0,
                    "broker_time_msc": (end - 60) * 1000,
                    "volume_lots": "0.01",
                    "price": "2399.50",
                    "profit": "0",
                    "commission": "-0.07",
                    "swap": "0",
                    "fee": "-0.01",
                }
            ],
        },
    }


@pytest.fixture
def receiver():
    clock = {"seconds": 100.0, "utc": NOW}
    client = TestClient(
        create_companion_app(
            token=TOKEN,
            account_id="123456",
            broker_server="Synthetic-Demo",
            symbol="XAUUSD.a",
            utc_now=lambda: clock["utc"],
            monotonic=lambda: clock["seconds"],
        )
    )
    return client, clock


def send(client, payload=None, **kwargs):
    return client.post(
        ROUTE, json=sample() if payload is None else payload, headers=HEADERS, **kwargs
    )


def test_roundtrip_preserves_broker_evidence_without_claiming_utc_or_import(receiver):
    client, _ = receiver
    assert client.get(HEALTH, headers=HEADERS).json()["status"] == "awaiting_terminal"
    assert client.get(ROUTE, headers=HEADERS).status_code == 503
    assert send(client).json() == {"accepted": True, "sequence": 1, "persisted": False}
    response = client.get(ROUTE, headers=HEADERS).json()
    assert response["status"] == "receiving"
    assert response["persisted"] is False
    assert response["quote_freshness"] == "not_verified"
    assert response["history_coverage"] == "recent_window_only"
    assert response["received_at"] == NOW.isoformat()
    data = response["snapshot"]
    assert data["quote"]["bid"] == "2399.1234567890"
    assert data["market_time_basis"] == "broker_server_unconverted"
    assert data["history"]["deals"][0] == sample()["history"]["deals"][0]


@pytest.mark.parametrize("path,method", [(HEALTH, "get"), (ROUTE, "get"), (ROUTE, "post")])
def test_all_routes_require_bearer_auth(receiver, path, method):
    client, _ = receiver
    call = getattr(client, method)
    assert call(path).status_code == 401
    assert call(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert call(f"{path}?token={TOKEN}").status_code == 401
    assert call(path, headers={**HEADERS, "Origin": "https://example.test"}).status_code == 403


def test_no_docs_or_order_surface(receiver):
    client, _ = receiver
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    for path in ("/v1/orders", "/v1/events", "/v1/account", "/v1/quote"):
        assert client.post(path, headers=HEADERS).status_code in (404, 405)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "999"),
        ("broker_server", "Different-Demo"),
    ],
)
def test_pins_account_and_server(receiver, field, value):
    client, _ = receiver
    body = sample()
    body[field] = value
    assert send(client, body).status_code == 409
    assert client.get(HEALTH, headers=HEADERS).json()["status"] == "awaiting_terminal"


def test_quote_symbol_is_exact_and_missing_quote_is_not_invented(receiver):
    client, _ = receiver
    body = sample()
    body["quote"]["symbol"] = "XAUUSD"
    assert send(client, body).status_code == 409
    body["quote"] = None
    assert send(client, body).status_code == 200
    assert client.get(HEALTH, headers=HEADERS).json()["quote_available"] is False


def test_replays_and_old_sequences_cannot_refresh_snapshot(receiver):
    client, clock = receiver
    assert send(client).status_code == 200
    clock["seconds"] += 30
    assert send(client).status_code == 409
    assert client.get(HEALTH, headers=HEADERS).json()["age_seconds"] == 30
    body = sample()
    body["sequence"] = 2
    assert send(client, body).status_code == 200
    assert send(client).status_code == 409


def test_stale_data_is_unavailable_even_if_wall_clock_moves_backwards(receiver):
    client, clock = receiver
    send(client)
    clock["utc"] -= timedelta(days=1)
    clock["seconds"] += 45
    assert client.get(HEALTH, headers=HEADERS).json()["status"] == "stale"
    response = client.get(ROUTE, headers=HEADERS)
    assert response.status_code == 503
    assert "123456" not in response.text


@pytest.mark.parametrize("offset", [-46, 46])
def test_rejects_stale_or_future_capture(receiver, offset):
    client, _ = receiver
    body = sample()
    body["captured_at"] = (NOW + timedelta(seconds=offset)).isoformat()
    assert send(client, body).status_code == 409


@pytest.mark.parametrize(
    "path,value",
    [
        (("captured_at",), "2026-09-22T12:00:00"),
        (("captured_at",), "2026-09-22T13:00:00+01:00"),
        (("market_time_basis",), "UTC"),
        (("terminal_connected",), False),
        (("read_only",), False),
        (("sequence",), True),
        (("quote", "bid"), "NaN"),
        (("quote", "ask"), "Infinity"),
        (("quote", "ask"), "100"),
        (("quote", "bid"), "-1"),
        (("quote", "broker_time_msc"), 0),
        (("account", "equity"), "1e100000"),
        (("account", "broker_password"), "private-fixture-must-not-echo"),
        (("history", "coverage"), "complete"),
        (("history", "truncated"), True),
        (("history", "total_deals"), 0),
        (("history", "broker_from_seconds"), 1),
        (("history", "broker_to_seconds"), 1),
    ],
)
def test_invalid_evidence_rejected_without_payload_echo(receiver, path, value):
    client, _ = receiver
    body = sample()
    target = body
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    result = send(client, body)
    assert result.status_code == 422
    assert "123456" not in result.text
    assert "private-fixture" not in result.text


def test_deals_bounded_with_truthful_partial_coverage(receiver):
    client, _ = receiver
    body = sample()
    deal = body["history"]["deals"][0]
    body["history"].update(
        total_deals=501,
        truncated=True,
        deals=[{**deal, "ticket": str(i + 1)} for i in range(500)],
    )
    assert send(client, body).status_code == 200
    body["sequence"] = 2
    body["history"]["deals"].append({**deal, "ticket": "501"})
    assert send(client, body).status_code == 422


def test_deal_validation_and_cash_movements(receiver):
    client, _ = receiver
    body = sample()
    cash = body["history"]["deals"][0]
    cash.update(deal_type=2, symbol="", volume_lots="0", price="0", profit="10000")
    assert send(client, body).status_code == 200
    body["sequence"] = 2
    body["history"]["deals"].append(copy.deepcopy(cash))
    body["history"]["total_deals"] = 2
    assert send(client, body).status_code == 422
    body["history"]["deals"].pop()
    body["history"]["total_deals"] = 1
    cash["broker_time_msc"] = 1
    assert send(client, body).status_code == 422


def test_empty_window_is_explicit_and_not_complete_history(receiver):
    client, _ = receiver
    body = sample()
    body["history"].update(total_deals=0, deals=[])
    assert send(client, body).status_code == 200
    assert client.get(ROUTE, headers=HEADERS).json()["history_coverage"] == "recent_window_only"


def test_size_content_type_and_parse_limits(receiver):
    client, _ = receiver
    assert client.post(ROUTE, content="{}", headers=HEADERS).status_code == 415
    headers = {**HEADERS, "Content-Type": "application/json"}
    assert client.post(ROUTE, content="{bad", headers=headers).status_code == 422
    assert (
        client.post(ROUTE, content=b"x" * (MAX_BODY_BYTES + 1), headers=headers).status_code == 413
    )
    headers["Content-Encoding"] = "gzip"
    assert client.post(ROUTE, content=json.dumps(sample()), headers=headers).status_code == 415


@pytest.mark.parametrize("token", ["short", "a" * 32 + "\r\n", "é" * 32])
def test_unsafe_tokens_rejected(token):
    with pytest.raises(ValueError):
        create_companion_app(token=token, account_id="123", broker_server="Demo", symbol="XAUUSD")


def test_launcher_prompts_without_env_or_database(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["trading-agent-mt5-companion"])
    answers = iter(["123456", "Synthetic-Demo", "XAUUSD.a"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    calls = []
    monkeypatch.setattr("app.metatrader_companion.uvicorn.run", lambda *a, **kw: calls.append(kw))
    run()
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["access_log"] is False
    assert calls[0]["proxy_headers"] is False
    assert "nothing saved to your journal" in capsys.readouterr().out


def test_mql_source_is_packaged_and_has_no_order_or_dll_calls():
    source = Path(__file__).parents[1] / "app/companions/TradingAgentReadOnly.mq5"
    text = source.read_text()
    # Source-level guardrail, not a substitute for MetaEditor compilation/live qualification.
    assert not re.search(r"\b(OrderSend|OrderSendAsync|OrderModify|OrderDelete)\s*\(", text)
    assert "#include" not in text and "#import" not in text
    assert '"http://127.0.0.1:"' in text
    assert "HistorySelect(since, until)" in text
    assert "PinnedAccount()" in text
    assert "EventSetTimer(10)" in text
    assert 'WebRequest("POST", url, headers, 3000' in text
    assert "CP_UTF8" in text


def test_mql_quote_subscribes_to_pinned_broker_symbol_before_reading():
    source = Path(__file__).parents[1] / "app/companions/TradingAgentReadOnly.mq5"
    text = source.read_text()
    quote = text.split("string BrokerQuote()", 1)[1].split("bool Snapshot", 1)[0]
    assert quote.index("SymbolExist(QuoteSymbol, custom)") < quote.index("SymbolSelect")
    assert quote.index("SymbolSelect(QuoteSymbol, true)") < quote.index("SymbolInfoTick")
    assert '|| custom)' in quote
    assert 'return "null"' in quote
    assert 'SymbolInfoTick(QuoteSymbol, tick)' in quote
    assert 'SymbolInfoTick(_Symbol' not in text
    assert "GetTickCount64()" in text
    assert "sequence++" not in text


def test_uptime_sequences_allow_reload_without_resetting_replay_protection(receiver):
    client, _ = receiver
    body = sample()
    body["sequence"] = 279
    assert send(client, body).status_code == 200
    body["sequence"] = 100000
    assert send(client, body).status_code == 200
    body["sequence"] = 110000
    assert send(client, body).status_code == 200
    assert send(client, body).status_code == 409
