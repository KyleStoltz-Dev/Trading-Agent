"""Adapt validated EA snapshots to the existing read-only broker tool contract.

No database or order access. Raw market times remain useful as explicitly labelled
evidence, but normalized market reads require an operator-confirmed server timezone.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

if TYPE_CHECKING:
    from app.metatrader_companion import CompanionSnapshot


def broker_time(milliseconds: int, zone: ZoneInfo | None) -> str:
    if zone is None:
        raise HTTPException(409, {"code": "broker_timezone_required"})
    wall = datetime.fromtimestamp(milliseconds / 1000, UTC).replace(tzinfo=None)
    candidates = {
        wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        for fold in (0, 1)
        if wall.replace(tzinfo=zone, fold=fold)
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == wall
    }
    if len(candidates) != 1:
        raise HTTPException(409, {"code": "ambiguous_broker_time"})
    return candidates.pop().isoformat()


def attach_broker_routes(
    app: FastAPI,
    current: Callable[[Request], "CompanionSnapshot"],
    *,
    symbol: str,
    broker_timezone: str | None,
    mailbox,
) -> None:
    try:
        zone = ZoneInfo(broker_timezone) if broker_timezone else None
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Invalid broker timezone; use a broker-confirmed IANA name") from None

    def require_symbol(instrument: str) -> None:
        if instrument != symbol:
            raise HTTPException(404, {"code": "symbol_not_subscribed"})

    @app.get("/v1/health")
    def health(request: Request):
        data = current(request)
        return {
            "account_id": data.account_id,
            "platform": "mt5",
            "read_only": True,
            "terminal_connected": True,
            "transport": "mql-companion",
            "broker_server": data.broker_server,
            "server_time": data.captured_at.isoformat(),
            "broker_timezone": broker_timezone,
            "history_coverage": "recent_window_only",
            "journal_import_available": False,
            "trade_allowed_by_bridge": False,
            "candle_reads": "on_demand",
        }

    @app.get("/v1/account")
    def account(request: Request):
        data = current(request)
        return {
            **data.account.model_dump(mode="json"),
            "account_id": data.account_id,
            "time": data.captured_at.isoformat(),
        }

    @app.get("/v1/positions")
    def positions(request: Request):
        data = current(request)
        if data.positions is None:
            raise HTTPException(409, {"code": "positions_unavailable"})
        # One record per symbol, matching existing ledger reconciliation semantics.
        groups: dict[str, list] = {}
        for position in data.positions:
            groups.setdefault(position.symbol, []).append(position)
        items = []
        for instrument, legs in groups.items():
            quantity = sum(
                (p.volume_lots if p.side == "buy" else -p.volume_lots for p in legs), Decimal(0)
            )
            average = None
            if len({p.side for p in legs}) == 1:
                average = sum((p.volume_lots * p.open_price for p in legs), Decimal(0)) / sum(
                    p.volume_lots for p in legs
                )
            items.append(
                {
                    "position_id": instrument,
                    "symbol": instrument,
                    "net_quantity": str(quantity),
                    "quantity_unit": "lots",
                    "average_price": str(average) if average is not None else None,
                    "unrealized_pnl": str(sum((p.unrealized_pnl for p in legs), Decimal(0))),
                    "time": data.captured_at.isoformat(),
                }
            )
        return {"account_id": data.account_id, "positions": items}

    @app.get("/v1/symbols")
    def symbols(request: Request):
        data = current(request)
        return {
            "account_id": data.account_id,
            "symbols": [
                {"symbol": symbol, "description": symbol, "path": "unknown", "tradable": False}
            ],
        }

    @app.get("/v1/quote")
    def quote(request: Request, instrument: str):
        data = current(request)
        require_symbol(instrument)
        if data.quote is None:
            raise HTTPException(409, {"code": "quote_unavailable"})
        return {
            "symbol": symbol,
            "bid": str(data.quote.bid),
            "ask": str(data.quote.ask),
            "time": broker_time(data.quote.broker_time_msc, zone),
        }

    async def read_candles(request, instrument, timeframe, count, before):
        from app.metatrader_candles import CandleRead, CandleReply

        data = current(request)
        require_symbol(instrument)
        try:
            query = CandleRead(timeframe=timeframe.upper(), count=count, before=before)
        except ValidationError:
            raise HTTPException(409, {"code": "invalid_candle_request"}) from None
        cached = next((s for s in data.candle_series if s.timeframe == query.timeframe), None)
        if not before and cached is not None and len(cached.candles) >= count:
            return CandleReply(
                request_id="0" * 32,
                account_id=data.account_id,
                broker_server=data.broker_server,
                symbol=symbol,
                request=query,
                captured_at=data.captured_at,
                status="ok",
                candles=cached.candles[-count:],
            )
        result = await mailbox.read(query)
        current(request)  # Do not serve a result after the terminal stream has gone stale.
        if result.status != "ok":
            raise HTTPException(409, {"code": "candles_unavailable"})
        return result

    @app.get("/v1/candles")
    async def candles(
        request: Request, instrument: str, timeframe: str, count: int, before: int = 0
    ):
        current(request)
        require_symbol(instrument)
        if zone is None:
            raise HTTPException(409, {"code": "broker_timezone_required"})
        result = await read_candles(request, instrument, timeframe, count, before)
        return {
            "candles": [
                {
                    "time": broker_time(bar.broker_time_seconds * 1000, zone),
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": str(bar.tick_volume),
                    "complete": bar.complete,
                }
                for bar in result.candles
            ],
            "volume_unit": "tick_count",
            "next_before_broker_time": result.candles[0].broker_time_seconds,
        }

    @app.get("/v1/companion/candles")
    async def raw_candles(
        request: Request, instrument: str, timeframe: str, count: int, before: int = 0
    ):
        result = await read_candles(request, instrument, timeframe, count, before)
        return {
            "account_id": result.account_id,
            "source": "metatrader-mt5-bridge",
            "instrument": result.symbol,
            "venue": result.broker_server,
            "timeframe": result.request.timeframe,
            "captured_at": result.captured_at.isoformat(),
            "market_time_basis": "broker_server_unconverted",
            "volume_unit": "tick_count",
            "requested_count": count,
            "returned_count": len(result.candles),
            "partial": len(result.candles) < count,
            "next_before_broker_time": result.candles[0].broker_time_seconds,
            "history_coverage": "requested_page_only",
            "candles": [
                {
                    **bar.model_dump(mode="json"),
                    "broker_wall_time": datetime.fromtimestamp(bar.broker_time_seconds, UTC)
                    .replace(tzinfo=None)
                    .isoformat(),
                }
                for bar in result.candles
            ],
        }

    @app.get("/v1/events")
    def events(request: Request):
        current(request)
        # A sliding window is not a lossless cursor. Refuse import rather than
        # advancing past missed executions or manufacturing opening trade legs.
        raise HTTPException(409, {"code": "companion_import_not_qualified"})

    @app.get("/v1/support-context")
    def support_context(request: Request):
        data = current(request)
        history = data.history
        deals = sorted(
            history.deals, key=lambda d: (d.broker_time_msc, int(d.ticket)), reverse=True
        )
        return {
            "account_id": data.account_id,
            "broker_server": data.broker_server,
            "source": "metatrader-mt5-bridge",
            "captured_at": data.captured_at.isoformat(),
            "market_time_basis": data.market_time_basis,
            "broker_timezone": broker_timezone,
            "quote_freshness": "not_verified",
            "quantity_unit": "lots",
            "volume_unit": "tick_count",
            "quote": data.quote.model_dump(mode="json") if data.quote else None,
            "positions": None
            if data.positions is None
            else [p.model_dump(mode="json") for p in data.positions],
            "candle_series": [
                {
                    "symbol": s.symbol,
                    "timeframe": s.timeframe,
                    "available_count": len(s.candles),
                    "candles": [bar.model_dump(mode="json") for bar in s.candles[-10:]],
                }
                for s in data.candle_series
            ],
            "history": {
                "coverage": history.coverage,
                "truncated_at_terminal": history.truncated,
                "broker_from_seconds": history.broker_from_seconds,
                "broker_to_seconds": history.broker_to_seconds,
                "total_deals_in_window": history.total_deals,
                "returned_deals": len(deals[:20]),
                "more_in_snapshot": len(deals) > 20,
                "deals": [
                    {
                        **d.model_dump(mode="json"),
                        "classification": "execution" if d.deal_type in (0, 1) else "non_execution",
                    }
                    for d in deals[:20]
                ],
            },
            "journal_import_available": False,
            "limitations": [
                "Raw broker times are not UTC; do not infer freshness from receipt time.",
                "Recent deals are not complete history or a closed-trade performance sample.",
                "Cash movements are not trade profits. Position/deal volumes are lots, not units.",
                "Cancelled and rejected orders are not included in deal history.",
            ],
        }
