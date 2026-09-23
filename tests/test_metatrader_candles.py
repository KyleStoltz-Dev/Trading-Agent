import asyncio
import copy

import httpx
import pytest
from fastapi import HTTPException

from app.metatrader_candles import MAX_CANDLE_BYTES, TIMEFRAMES, CandleMailbox, CandleRead
from app.metatrader_companion import create_companion_app
from tests.test_metatrader_companion import HEADERS, NOW, ROUTE, TOKEN, sample


def make_app():
    return create_companion_app(
        token=TOKEN,
        account_id="123456",
        broker_server="Synthetic-Demo",
        symbol="XAUUSD.a",
        utc_now=lambda: NOW,
    )


def reply(wire, count=75):
    key, timeframe, requested, before = wire.split("|")
    end = int(before) - 1 if int(before) else int(NOW.timestamp())
    return {
        "request_id": key,
        "account_id": "123456",
        "broker_server": "Synthetic-Demo",
        "symbol": "XAUUSD.a",
        "request": {"timeframe": timeframe, "count": int(requested), "before": int(before)},
        "captured_at": NOW.isoformat(),
        "status": "ok",
        "candles": [
            {
                "broker_time_seconds": end - (count - i - 1) * 60,
                "open": "2000",
                "high": "2001",
                "low": "1999",
                "close": "2000.5",
                "tick_volume": 4,
                "complete": bool(int(before)) or i < count - 1,
            }
            for i in range(count)
        ],
    }


async def poll(client):
    for _ in range(50):
        result = await client.get("/v1/companion/candle-request")
        if result.status_code == 200:
            return result.text
        await asyncio.sleep(0)
    pytest.fail("No candle read queued")


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_all_mt5_timeframes_on_demand_and_stable_older_page(timeframe):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=make_app()), base_url="http://test", headers=HEADERS
        ) as client:
            assert (await client.post(ROUTE, json=sample())).status_code == 200
            before = 0
            for _ in range(2):
                pending = asyncio.create_task(
                    client.get(
                        "/v1/companion/candles",
                        params={
                            "instrument": "XAUUSD.a",
                            "timeframe": timeframe,
                            "count": 100,
                            "before": before,
                        },
                    )
                )
                wire = await poll(client)
                data = reply(wire)
                assert (
                    await client.post("/v1/companion/candle-result", json=data)
                ).status_code == 200
                response = await pending
                assert response.status_code == 200
                body = response.json()
                assert len(body["candles"]) == 75
                assert body["partial"] is True
                assert body["timeframe"] == timeframe
                assert body["market_time_basis"] == "broker_server_unconverted"
                assert body["candles"][0]["broker_wall_time"].endswith("Z") is False
                if before:
                    assert max(c["broker_time_seconds"] for c in body["candles"]) < before
                before = body["next_before_broker_time"]
                assert (
                    await client.post("/v1/companion/candle-result", json=data)
                ).status_code == 409
            assert (await client.get("/v1/companion/candle-request")).status_code == 204

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "defect",
    ["account", "server", "symbol", "request", "id", "future", "ohlc", "duplicate", "before"],
)
def test_result_must_match_pending_read_and_valid_evidence(defect):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=make_app()), base_url="http://test", headers=HEADERS
        ) as client:
            await client.post(ROUTE, json=sample())
            pending = asyncio.create_task(
                client.get(
                    "/v1/companion/candles",
                    params={
                        "instrument": "XAUUSD.a",
                        "timeframe": "D1",
                        "count": 100,
                        "before": 1700000000,
                    },
                )
            )
            data = reply(await poll(client))
            invalid = copy.deepcopy(data)
            if defect == "account":
                invalid["account_id"] = "999"
            elif defect == "server":
                invalid["broker_server"] = "Wrong"
            elif defect == "symbol":
                invalid["symbol"] = "EURUSD"
            elif defect == "request":
                invalid["request"]["timeframe"] = "H1"
            elif defect == "id":
                invalid["request_id"] = "f" * 32
            elif defect == "future":
                invalid["captured_at"] = "2099-01-01T00:00:00Z"
            elif defect == "ohlc":
                invalid["candles"][0]["high"] = "1"
            elif defect == "duplicate":
                invalid["candles"][0] = invalid["candles"][1]
            else:
                invalid["candles"][-1]["broker_time_seconds"] = 1700000000
            assert (await client.post("/v1/companion/candle-result", json=invalid)).status_code in (
                409,
                422,
            )
            assert not pending.done()
            await client.post("/v1/companion/candle-result", json=data)
            assert (await pending).status_code == 200

    asyncio.run(scenario())


def test_read_timeout_cancellation_and_capacity_release():
    async def scenario():
        mailbox = CandleMailbox(wait_seconds=0.01)
        query = CandleRead(timeframe="MN1", count=5000)
        tasks = [asyncio.create_task(mailbox.read(query)) for _ in range(4)]
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as error:
            await mailbox.read(query)
        assert error.value.detail["code"] == "candle_reader_busy"
        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not mailbox.pending
        with pytest.raises(HTTPException) as error:
            await mailbox.read(query)
        assert error.value.detail["code"] == "candle_read_pending"
        assert not mailbox.pending

    asyncio.run(scenario())


def test_candle_routes_auth_origin_size_and_parameter_bounds():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
        ) as client:
            await client.post(ROUTE, json=sample(), headers=HEADERS)
            for method, route in [
                ("GET", "/v1/companion/candle-request"),
                ("POST", "/v1/companion/candle-result"),
                ("GET", "/v1/companion/candles?instrument=XAUUSD.a&timeframe=D1&count=100"),
            ]:
                assert (await client.request(method, route)).status_code == 401
                assert (
                    await client.request(
                        method, route, headers={**HEADERS, "Origin": "https://test"}
                    )
                ).status_code == 403
            for timeframe, count in [("code()", 50), ("H1", 5001), ("D1", 0)]:
                result = await client.get(
                    "/v1/companion/candles",
                    params={
                        "instrument": "XAUUSD.a",
                        "timeframe": timeframe,
                        "count": count,
                    },
                    headers=HEADERS,
                )
                assert result.status_code == 409
            result = await client.post(
                "/v1/companion/candle-result",
                content=b"x" * (MAX_CANDLE_BYTES + 1),
                headers={**HEADERS, "Content-Type": "application/json"},
            )
            assert result.status_code == 413

    asyncio.run(scenario())
