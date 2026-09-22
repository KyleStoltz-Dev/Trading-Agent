"""Loopback-only receiver for the experimental MQL5 companion.

This is a read-only transport. Nothing is persisted or
sent back to the terminal except acknowledgement; there is no execution surface.
"""

import argparse
import asyncio
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

MAX_BODY_BYTES = 1_048_576
MAX_DEALS = 500
STALE_SECONDS = 45
Symbol = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")]
Identifier = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]
Finite = Annotated[Decimal, Field(allow_inf_nan=False, max_digits=30, decimal_places=12)]
Positive = Annotated[Finite, Field(gt=0)]
Nonnegative = Annotated[Finite, Field(ge=0)]
ServerTime = Annotated[int, Field(strict=True, gt=0, le=253402300799999)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountSnapshot(Payload):
    currency: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,12}$")]
    balance: Finite
    equity: Finite
    margin_used: Nonnegative
    margin_available: Finite


class QuoteSnapshot(Payload):
    symbol: Symbol
    bid: Positive
    ask: Positive
    broker_time_msc: ServerTime

    @model_validator(mode="after")
    def validate_spread(self):
        if self.ask < self.bid:
            raise ValueError("Crossed quote")
        return self


class DealSnapshot(Payload):
    ticket: Identifier
    order_ticket: Identifier
    position_id: Identifier
    # Balance/credit deals may have no symbol; they are retained, not called fills.
    symbol: Annotated[str, Field(max_length=80, pattern=r"^[A-Za-z0-9._:/-]*$")]
    deal_type: Annotated[int, Field(strict=True, ge=0, le=100)]
    entry_type: Annotated[int, Field(strict=True, ge=0, le=3)]
    broker_time_msc: ServerTime
    volume_lots: Nonnegative
    price: Nonnegative
    profit: Finite
    commission: Finite
    swap: Finite
    fee: Finite


class RecentHistory(Payload):
    coverage: Literal["recent_window"]
    broker_from_seconds: Annotated[int, Field(strict=True, gt=0)]
    broker_to_seconds: Annotated[int, Field(strict=True, gt=0)]
    total_deals: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
    truncated: bool
    deals: Annotated[list[DealSnapshot], Field(max_length=MAX_DEALS)]

    @model_validator(mode="after")
    def validate_history(self):
        start, end = self.broker_from_seconds, self.broker_to_seconds
        if not 0 <= end - start <= 7 * 86400:
            raise ValueError("History must cover at most seven days")
        if len(self.deals) != min(self.total_deals, MAX_DEALS):
            raise ValueError("History count mismatch")
        if self.truncated != (self.total_deals > MAX_DEALS):
            raise ValueError("Incorrect truncation flag")
        if len({deal.ticket for deal in self.deals}) != len(self.deals):
            raise ValueError("Duplicate deal tickets")
        if any(not start * 1000 <= d.broker_time_msc < (end + 1) * 1000 for d in self.deals):
            raise ValueError("Deal outside requested history window")
        return self


class PositionSnapshot(Payload):
    ticket: Identifier
    symbol: Symbol
    side: Literal["buy", "sell"]
    volume_lots: Positive
    open_price: Positive
    unrealized_pnl: Finite


class CandleSnapshot(Payload):
    broker_time_seconds: Annotated[int, Field(strict=True, gt=0, le=253402300799)]
    open: Positive
    high: Positive
    low: Positive
    close: Positive
    tick_volume: Annotated[int, Field(strict=True, ge=0)]
    complete: bool

    @model_validator(mode="after")
    def validate_prices(self):
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("Invalid candle high")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("Invalid candle low")
        return self


class CandleSeries(Payload):
    symbol: Symbol
    timeframe: Literal["H4", "M15", "M5", "M1"]
    candles: Annotated[list[CandleSnapshot], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def validate_order(self):
        times = [bar.broker_time_seconds for bar in self.candles]
        if times != sorted(set(times)):
            raise ValueError("Candles must be distinct and chronological")
        if any(not bar.complete for bar in self.candles[:-1]):
            raise ValueError("Only the latest candle may be incomplete")
        # The latest bar is conservatively incomplete; do not infer closure from
        # receive time, especially on weekends and disconnected markets.
        if self.candles[-1].complete:
            raise ValueError("Latest candle must remain provisional")
        return self


class CompanionSnapshot(Payload):
    schema_version: Literal[1]
    companion_build: Annotated[str, Field(pattern=r"^(legacy|development|[a-f0-9]{64})$")] = (
        "legacy"
    )
    platform: Literal["mt5"]
    read_only: Literal[True]
    terminal_connected: Literal[True]
    account_id: Identifier
    broker_server: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    ]
    sequence: Annotated[int, Field(strict=True, gt=0, le=9_223_372_036_854_775_807)]
    captured_at: datetime
    # Raw server-clock values are deliberately NOT converted to UTC.
    market_time_basis: Literal["broker_server_unconverted"]
    account: AccountSnapshot
    quote: QuoteSnapshot | None
    history: RecentHistory
    # None means an old companion or a failed read, NEVER an empty account.
    positions: Annotated[list[PositionSnapshot] | None, Field(max_length=500)] = None
    candle_series: Annotated[list[CandleSeries], Field(max_length=4)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_time(self):
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset().total_seconds() != 0:
            raise ValueError("Capture time must have an explicit UTC offset")
        if self.positions is not None and len({p.ticket for p in self.positions}) != len(
            self.positions
        ):
            raise ValueError("Duplicate position tickets")
        if len({s.timeframe for s in self.candle_series}) != len(self.candle_series):
            raise ValueError("Duplicate candle series")
        return self


def create_companion_app(
    *,
    token: str,
    account_id: str,
    broker_server: str,
    symbol: str,
    broker_timezone: str | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """A single pinned account, one bounded snapshot, no disk/database writes."""
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Use a dedicated random token of at least 32 characters")
    # Validate pins using the same constraints as incoming records.
    TypeAdapter(Identifier).validate_python(account_id)
    TypeAdapter(Symbol).validate_python(symbol)
    if (
        not broker_server
        or len(broker_server) > 128
        or any(ord(c) < 32 or ord(c) == 127 for c in broker_server)
    ):
        raise ValueError("Use the exact MT5 broker server name")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    lock = threading.Lock()
    snapshot: CompanionSnapshot | None = None
    received_at: datetime | None = None
    received_clock = 0.0

    def authenticate(request: Request) -> None:
        # No browser clients/CORS or query-string tokens. Do not trust forwarded IPs.
        if request.headers.get("origin") is not None:
            raise HTTPException(403, "Browser requests are not supported")
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not secrets.compare_digest(supplied, f"Bearer {token}".encode()):
            raise HTTPException(401, "Companion authentication required")

    def status() -> dict:
        age = max(0.0, monotonic() - received_clock) if snapshot else None
        return {
            "status": "awaiting_terminal"
            if age is None
            else ("stale" if age >= STALE_SECONDS else "receiving"),
            "read_only": True,
            "persisted": False,
            "received_at": received_at.isoformat() if received_at else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "quote_available": snapshot is not None and snapshot.quote is not None,
            "quote_freshness": "not_verified",
            "history_coverage": "recent_window_only",
            "sequence": snapshot.sequence if snapshot else None,
            "companion_build": snapshot.companion_build if snapshot else None,
        }

    @app.post("/v1/companion/snapshot")
    async def receive(request: Request):
        nonlocal snapshot, received_at, received_clock
        authenticate(request)
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "JSON is required")
        if request.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(415, "Compressed payloads are not supported")
        data = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_BODY_BYTES:
                        raise HTTPException(413, "Companion snapshot is too large")
                    data.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Companion upload timed out") from None
        try:
            candidate = CompanionSnapshot.model_validate_json(data)
        except (ValidationError, ValueError):
            # Never echo private account data in errors or logs.
            raise HTTPException(
                422, "Invalid companion snapshot; check schema and values"
            ) from None
        if candidate.account_id != account_id or candidate.broker_server != broker_server:
            raise HTTPException(409, "Account or broker server does not match this receiver")
        if candidate.quote and candidate.quote.symbol != symbol:
            raise HTTPException(409, "Quote symbol does not match this receiver")
        if any(series.symbol != symbol for series in candidate.candle_series):
            raise HTTPException(409, "Candle symbol does not match this receiver")
        now = utc_now()
        if abs((now - candidate.captured_at).total_seconds()) > STALE_SECONDS:
            raise HTTPException(409, "Capture time is stale or clock is incorrect")
        with lock:
            if snapshot and candidate.sequence <= snapshot.sequence:
                raise HTTPException(409, "Duplicate or out-of-order snapshot")
            snapshot, received_at, received_clock = candidate, now, monotonic()
        return {"accepted": True, "sequence": candidate.sequence, "persisted": False}

    @app.get("/v1/companion/health")
    def health(request: Request):
        authenticate(request)
        with lock:
            return status()

    @app.get("/v1/companion/snapshot")
    def read(request: Request):
        authenticate(request)
        with lock:
            current = status()
            if current["status"] != "receiving":
                raise HTTPException(503, "No recent terminal snapshot; keep MT5 open and connected")
            return {**current, "snapshot": snapshot.model_dump(mode="json")}

    def current_snapshot(request: Request) -> CompanionSnapshot:
        authenticate(request)
        with lock:
            if status()["status"] != "receiving":
                raise HTTPException(503, "No recent terminal snapshot; keep MT5 open and connected")
            return snapshot

    from app.metatrader_companion_api import attach_broker_routes

    attach_broker_routes(app, current_snapshot, symbol=symbol, broker_timezone=broker_timezone)
    return app


def run() -> None:
    parser = argparse.ArgumentParser(description="Experimental read-only MT5 local companion")
    parser.add_argument("--account", help="Exact MT5 login number (not your password)")
    parser.add_argument("--server", help="Exact broker server name shown in MT5")
    parser.add_argument("--symbol", help="Exact gold symbol shown in MT5, including any suffix")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--preset", type=Path, help="Reuse a private EA preset; no token rotation")
    parser.add_argument(
        "--broker-timezone", help="Broker-confirmed IANA server timezone; never your Mac timezone"
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    try:
        if args.preset:
            from app.metatrader_pairing import read_pairing

            if args.account or args.server or args.symbol or args.port != 8766:
                parser.error("Use --preset by itself; account and port come from the saved pairing")
            pairing = read_pairing(args.preset)
            account, server, symbol = pairing.account, pairing.server, pairing.symbol
            token, args.port = pairing.token, pairing.port
        else:
            account = args.account or input("MT5 login number: ").strip()
            server = args.server or input("MT5 broker server name: ").strip()
            symbol = args.symbol or input("Gold symbol shown in MT5: ").strip()
            token = secrets.token_urlsafe(32)
        app = create_companion_app(
            token=token,
            account_id=account,
            broker_server=server,
            symbol=symbol,
            broker_timezone=args.broker_timezone,
        )
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. Nothing was saved.")
        return
    except (ValueError, OSError):
        parser.error("Invalid account, server or symbol. Copy the exact values from MT5.")
    source = Path(__file__).with_name("companions") / "TradingAgentReadOnly.mq5"
    print("\nMT5 companion · read-only connection test · nothing saved to your journal")
    print(f"EA source: {source}")
    print(f"Allowlist in MT5: http://127.0.0.1:{args.port}")
    print(f"ReceiverPort: {args.port}")
    print(f"ExpectedAccount: {account}\nExpectedServer: {server}\nQuoteSymbol: {symbol}")
    if args.preset:
        print("Saved local pairing reused; no token changes or preset re-entry needed.")
    else:
        print(f"ReceiverToken (private; copy into the EA): {token}")
    print("Keep this terminal open. Reuse --preset on restart to keep the same pairing.")
    print("See docs/metatrader-mql-companion.md for compilation and verification steps.")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        proxy_headers=False,
        limit_concurrency=8,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    run()
