import asyncio
import copy
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.connectors.metatrader_bridge import (
    MetaTraderBridgeError,
    MetaTraderReadOnlyBridgeConnector,
)
from app.metatrader_companion import create_companion_app
from app.metatrader_companion_api import broker_time
from tests.test_metatrader_companion import HEADERS, NOW, ROUTE, TOKEN, sample


def evidence():
    data = sample()
    data["positions"] = [
        {
            "ticket": "41",
            "symbol": "XAUUSD.a",
            "side": "buy",
            "volume_lots": "0.1",
            "open_price": "2380",
            "unrealized_pnl": "10",
        },
        {
            "ticket": "42",
            "symbol": "XAUUSD.a",
            "side": "sell",
            "volume_lots": "0.1",
            "open_price": "2385",
            "unrealized_pnl": "-5",
        },
    ]
    data["candle_series"] = [
        {
            "symbol": "XAUUSD.a",
            "timeframe": "H4",
            "candles": [
                {
                    "broker_time_seconds": int(NOW.timestamp()) - 14400,
                    "open": "2390",
                    "high": "2400",
                    "low": "2380",
                    "close": "2395",
                    "tick_volume": 123,
                    "complete": True,
                },
                {
                    "broker_time_seconds": int(NOW.timestamp()),
                    "open": "2395",
                    "high": "2400",
                    "low": "2390",
                    "close": "2399",
                    "tick_volume": 17,
                    "complete": False,
                },
            ],
        }
    ]
    return data


def setup(*, timezone=None, data=None):
    clock = {"seconds": 100.0}
    app = create_companion_app(
        token=TOKEN,
        account_id="123456",
        broker_server="Synthetic-Demo",
        symbol="XAUUSD.a",
        broker_timezone=timezone,
        utc_now=lambda: NOW,
        monotonic=lambda: clock["seconds"],
    )
    web = TestClient(app)
    assert (
        web.post(ROUTE, headers=HEADERS, json=evidence() if data is None else data).status_code
        == 200
    )
    connector = MetaTraderReadOnlyBridgeConnector(
        base_url="http://127.0.0.1:8766",
        token=TOKEN,
        account_id="123456",
        platform="mt5",
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8766",
            headers=HEADERS,
        ),
    )
    return web, connector, clock


def test_existing_connector_reads_account_positions_quote_candles_and_context():
    web, connector, _ = setup(timezone="Etc/GMT-3")

    async def read():
        assert (await connector.health())["transport"] == "mql-companion"
        account = await connector.account()
        assert account.balance == Decimal("10000")
        assert account.market_time == NOW
        positions = await connector.positions()
        assert len(positions) == 1
        assert positions[0].net_quantity == 0  # hedge, not an empty account
        assert positions[0].average_price is None
        assert positions[0].unrealized_pnl == 5
        quote = await connector.latest_quote("XAUUSD.a")
        assert quote.bid == Decimal("2399.1234567890")
        assert quote.market_time.hour == 9
        candles = await connector.candles("XAUUSD.a", "H4", count=2)
        assert [bar.complete for bar in candles] == [True, False]
        assert candles[0].volume == 123
        assert candles[0].started_at.hour == 5
        context = await connector.support_context()
        assert context["quantity_unit"] == "lots"
        assert len(context["positions"]) == 2
        assert context["history"]["coverage"] == "recent_window"
        assert context["quote_freshness"] == "not_verified"
        assert context["journal_import_available"] is False
        assert (await connector.instruments())[0].symbol == "XAUUSD.a"
        await connector._client.aclose()

    asyncio.run(read())
    web.close()


def test_unverified_timezone_does_not_block_raw_evidence_or_invent_utc():
    _, connector, _ = setup()

    async def read():
        assert (await connector.account()).equity == Decimal("10001")
        with pytest.raises(MetaTraderBridgeError, match="timezone is not verified"):
            await connector.latest_quote("XAUUSD.a")
        with pytest.raises(MetaTraderBridgeError, match="timezone is not verified"):
            await connector.candles("XAUUSD.a", "H4", count=1)
        context = await connector.support_context()
        assert context["market_time_basis"] == "broker_server_unconverted"
        assert context["quote"]["broker_time_msc"] == sample()["quote"]["broker_time_msc"]
        with pytest.raises(MetaTraderBridgeError, match="Nothing was imported"):
            await connector.events_since(None)
        with pytest.raises(MetaTraderBridgeError, match="Nothing was imported"):
            await connector.events_since("old:cursor")

    asyncio.run(read())


@pytest.mark.parametrize(
    "route",
    [
        "/v1/health",
        "/v1/account",
        "/v1/positions",
        "/v1/symbols",
        "/v1/events",
        "/v1/quote?instrument=XAUUSD.a",
        "/v1/candles?instrument=XAUUSD.a&timeframe=H4&count=1",
        "/v1/support-context",
    ],
)
def test_all_broker_reads_reject_missing_auth_browser_and_stale_data(route):
    web, _, clock = setup(timezone="UTC")
    assert web.get(route).status_code == 401
    assert web.get(route, headers={**HEADERS, "Origin": "https://example.test"}).status_code == 403
    clock["seconds"] += 45
    assert web.get(route, headers=HEADERS).status_code == 503


def test_missing_positions_are_not_empty_and_missing_series_not_fabricated():
    web, connector, _ = setup(data=sample())
    with pytest.raises(MetaTraderBridgeError, match="unknown positions"):
        asyncio.run(connector.positions())
    assert web.get("/v1/quote?instrument=EURUSD", headers=HEADERS).status_code == 404
    assert (
        web.get(
            "/v1/candles?instrument=XAUUSD.a&timeframe=M5&count=50", headers=HEADERS
        ).status_code
        == 409
    )
    assert (
        web.get(
            "/v1/candles?instrument=XAUUSD.a&timeframe=H4&count=51", headers=HEADERS
        ).status_code
        == 409
    )


def test_dst_gap_and_fold_are_rejected_and_seasonal_offsets_preserved():
    zone = ZoneInfo("Europe/Helsinki")

    def raw(value):
        return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)

    assert broker_time(raw("2026-01-15T12:00:00"), zone).startswith("2026-01-15T10:")
    assert broker_time(raw("2026-07-15T12:00:00"), zone).startswith("2026-07-15T09:")
    for value in ("2026-03-29T03:30:00", "2026-10-25T03:30:00"):
        with pytest.raises(HTTPException) as error:
            broker_time(raw(value), zone)
        assert error.value.detail["code"] == "ambiguous_broker_time"


def test_recent_deals_distinguish_funding_and_are_bounded():
    data = evidence()
    data["history"]["deals"] = [
        {**data["history"]["deals"][0], "ticket": str(8000 + i), "deal_type": 2 if i == 24 else 0}
        for i in range(25)
    ]
    data["history"]["total_deals"] = 25
    web, _, _ = setup(data=data)
    history = web.get("/v1/support-context", headers=HEADERS).json()["history"]
    assert history["total_deals_in_window"] == 25
    assert history["returned_deals"] == 20
    assert history["more_in_snapshot"] is True
    assert history["deals"][0]["classification"] == "non_execution"


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate_position",
        "duplicate_series",
        "wrong_symbol",
        "bad_ohlc",
        "reordered",
        "closed_latest",
    ],
)
def test_invalid_new_evidence_is_rejected_without_replacing_valid_snapshot(defect):
    web, _, _ = setup()
    data = copy.deepcopy(evidence())
    data["sequence"] = 2
    if defect == "duplicate_position":
        data["positions"].append(data["positions"][0])
    elif defect == "duplicate_series":
        data["candle_series"].append(data["candle_series"][0])
    elif defect == "wrong_symbol":
        data["candle_series"][0]["symbol"] = "EURUSD"
    elif defect == "bad_ohlc":
        data["candle_series"][0]["candles"][0]["high"] = "2"
    elif defect == "reordered":
        data["candle_series"][0]["candles"].reverse()
    else:
        data["candle_series"][0]["candles"][-1]["complete"] = True
    assert web.post(ROUTE, headers=HEADERS, json=data).status_code in (409, 422)
    assert web.get(ROUTE, headers=HEADERS).json()["snapshot"]["sequence"] == 1
