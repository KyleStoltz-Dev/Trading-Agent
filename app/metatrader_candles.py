"""Bounded, typed candle-read mailbox. No arbitrary commands or execution surface."""

import asyncio
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import Field, ValidationError, field_validator, model_validator

from app.metatrader_companion import CandleSnapshot, Identifier, Payload, Symbol

TIMEFRAMES = (
    "M1",
    "M2",
    "M3",
    "M4",
    "M5",
    "M6",
    "M10",
    "M12",
    "M15",
    "M20",
    "M30",
    "H1",
    "H2",
    "H3",
    "H4",
    "H6",
    "H8",
    "H12",
    "D1",
    "W1",
    "MN1",
)
MAX_CANDLES = 5000
MAX_CANDLE_BYTES = 2_000_000


class CandleRead(Payload):
    timeframe: str
    count: Annotated[int, Field(strict=True, ge=1, le=MAX_CANDLES)]
    before: Annotated[int, Field(strict=True, ge=0, le=4102444800)] = 0

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value):
        if value not in TIMEFRAMES:
            raise ValueError("Unsupported MT5 timeframe")
        return value


class CandleReply(Payload):
    request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    account_id: Identifier
    broker_server: Annotated[str, Field(min_length=1, max_length=128)]
    symbol: Symbol
    request: CandleRead
    captured_at: datetime
    status: Literal["ok", "unavailable"]
    candles: Annotated[list[CandleSnapshot], Field(max_length=MAX_CANDLES)]

    @model_validator(mode="after")
    def validate_reply(self):
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset().total_seconds() != 0:
            raise ValueError("UTC capture time required")
        if len(self.candles) > self.request.count:
            raise ValueError("Too many candles")
        times = [c.broker_time_seconds for c in self.candles]
        if times != sorted(set(times)):
            raise ValueError("Candles must be distinct and chronological")
        if self.request.before and any(t >= self.request.before for t in times):
            raise ValueError("Candle outside requested page")
        if any(not c.complete for c in self.candles[:-1]):
            raise ValueError("Only the latest bar may be provisional")
        if (self.status == "ok") != bool(self.candles):
            raise ValueError("Result status mismatch")
        return self


class CandleMailbox:
    """All mailbox access runs on the application's event loop; at most four reads."""

    def __init__(self, *, wait_seconds: float = 8):
        self.pending: dict[str, tuple[CandleRead, asyncio.Future]] = {}
        self.wait_seconds = wait_seconds

    async def read(self, request: CandleRead) -> CandleReply:
        if len(self.pending) >= 4:
            raise HTTPException(409, {"code": "candle_reader_busy"})
        key = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = (request, future)
        try:
            async with asyncio.timeout(self.wait_seconds):
                return await future
        except TimeoutError:
            raise HTTPException(409, {"code": "candle_read_pending"}) from None
        finally:
            self.pending.pop(key, None)


def attach_candle_mailbox(
    app: FastAPI,
    *,
    authenticate: Callable,
    current: Callable,
    account_id: str,
    broker_server: str,
    symbol: str,
    utc_now: Callable,
) -> CandleMailbox:
    mailbox = CandleMailbox()

    @app.get("/v1/companion/candle-request")
    async def poll(request: Request):
        current(request)
        for key, (query, future) in mailbox.pending.items():
            if not future.done():
                # Strict ASCII fields only, no URL/path/symbol/code supplied to EA.
                return PlainTextResponse(f"{key}|{query.timeframe}|{query.count}|{query.before}")
        return Response(status_code=204)

    @app.post("/v1/companion/candle-result")
    async def submit(request: Request):
        authenticate(request)
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "JSON is required")
        if request.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(415, "Compressed payloads are not supported")
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_CANDLE_BYTES:
                        raise HTTPException(413, "Candle response exceeds limit")
                    body.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Candle upload timed out") from None
        try:
            result = CandleReply.model_validate_json(body)
        except (ValidationError, ValueError):
            raise HTTPException(422, "Invalid candle response") from None
        pending = mailbox.pending.get(result.request_id)
        if (
            result.account_id != account_id
            or result.broker_server != broker_server
            or result.symbol != symbol
            or pending is None
            or pending[0] != result.request
            or pending[1].done()
            or abs((utc_now() - result.captured_at).total_seconds()) > 45
        ):
            raise HTTPException(409, "Candle response does not match a current read")
        pending[1].set_result(result)
        return {"accepted": True, "persisted": False}

    return mailbox
