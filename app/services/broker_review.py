"""Deterministic summaries of normalized, read-only broker trade history."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Fill, Instrument, Trade, TradingAccount
from app.services.workspaces import RequestScope, validate_scope


@dataclass(frozen=True)
class BrokerTradeReviewRow:
    trade_id: str
    instrument: str
    side: str
    quantity: Decimal
    quantity_unit: str
    opened_at: datetime
    closed_at: datetime
    held_seconds: int
    realized_pnl: Decimal | None
    costs: Decimal
    net_pnl: Decimal | None
    account_currency: str


@dataclass(frozen=True)
class HoldingBucket:
    label: str
    trade_count: int
    average_net_pnl: Decimal | None


@dataclass(frozen=True)
class BrokerTradeReview:
    trades: tuple[BrokerTradeReviewRow, ...]
    trade_count: int
    winners: int
    losers: int
    unknown_outcomes: int
    net_pnl: Decimal | None
    account_currency: str
    quantity_unit: str
    holding_buckets: tuple[HoldingBucket, ...]
    as_of: datetime | None
    source: str

    def model_payload(self) -> dict:
        return asdict(self)


def _sum_optional(values: list[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    return sum(present, Decimal("0")) if present else None


def _cost_total(fills: list[Fill]) -> Decimal:
    fields = (
        "commission",
        "financing",
        "guaranteed_execution_fee",
        "half_spread_cost",
    )
    return sum(
        (
            getattr(fill, field)
            for fill in fills
            for field in fields
            if getattr(fill, field) is not None
        ),
        Decimal("0"),
    )


def _opening_quantity(trade: Trade, fills: list[Fill]) -> Decimal:
    opening_side = "buy" if trade.direction == "long" else "sell"
    return sum(
        (fill.quantity for fill in fills if fill.side == opening_side),
        Decimal("0"),
    )


def _bucket(rows: tuple[BrokerTradeReviewRow, ...], label: str, low: int, high: int | None):
    selected = [
        row
        for row in rows
        if row.held_seconds >= low and (high is None or row.held_seconds < high)
    ]
    known = [row.net_pnl for row in selected if row.net_pnl is not None]
    average = (
        (sum(known, Decimal("0")) / Decimal(len(known)))
        if known
        else None
    )
    return HoldingBucket(label=label, trade_count=len(selected), average_net_pnl=average)


def summarize_broker_trades(
    records: list[tuple[Trade, Instrument, list[Fill]]],
    *,
    account_currency: str,
) -> BrokerTradeReview:
    rows: list[BrokerTradeReviewRow] = []
    for trade, instrument, fills in records:
        if trade.opened_at is None or trade.closed_at is None:
            continue
        realized = _sum_optional([fill.realized_pnl for fill in fills])
        costs = _cost_total(fills)
        net = realized + costs if realized is not None else None
        rows.append(
            BrokerTradeReviewRow(
                trade_id=str(trade.id),
                instrument=instrument.canonical_symbol,
                side=trade.direction,
                quantity=_opening_quantity(trade, fills),
                quantity_unit="broker-reported units",
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                held_seconds=max(0, int((trade.closed_at - trade.opened_at).total_seconds())),
                realized_pnl=realized,
                costs=costs,
                net_pnl=net,
                account_currency=account_currency,
            )
        )
    ordered = tuple(sorted(rows, key=lambda row: (row.held_seconds, row.closed_at)))
    known = [row.net_pnl for row in ordered if row.net_pnl is not None]
    return BrokerTradeReview(
        trades=ordered,
        trade_count=len(ordered),
        winners=sum(value > 0 for value in known),
        losers=sum(value < 0 for value in known),
        unknown_outcomes=len(ordered) - len(known),
        net_pnl=sum(known, Decimal("0")) if known else None,
        account_currency=account_currency,
        quantity_unit="broker-reported units (not assumed to be lots)",
        holding_buckets=(
            _bucket(ordered, "under 1 minute", 0, 60),
            _bucket(ordered, "1 to 5 minutes", 60, 300),
            _bucket(ordered, "5 to 20 minutes", 300, 1200),
            _bucket(ordered, "over 20 minutes", 1200, None),
        ),
        as_of=max((fill.ingested_at for _, _, fills in records for fill in fills), default=None),
        source="normalized broker fills stored by Trading Agent",
    )


def broker_trade_review(
    db: Session,
    *,
    scope: RequestScope,
    limit: int = 50,
    days: int | None = None,
) -> BrokerTradeReview:
    """Review completed broker-imported trades without requesting new broker data."""
    validate_scope(db, scope)
    account = db.scalar(
        select(TradingAccount).where(
            TradingAccount.workspace_id == scope.workspace_id,
            TradingAccount.id == scope.account_id,
        )
    )
    if account is None:
        raise LookupError("selected trading account no longer exists")
    statement = (
        select(Trade, Instrument)
        .join(Instrument, Instrument.id == Trade.instrument_id)
        .where(
            Trade.workspace_id == scope.workspace_id,
            Trade.account_id == scope.account_id,
            Trade.origin == "broker_import",
            Trade.status == "closed",
            Trade.opened_at.is_not(None),
            Trade.closed_at.is_not(None),
        )
        .order_by(Trade.closed_at.desc())
        .limit(limit)
    )
    if days is not None:
        earliest_close = datetime.now().astimezone() - timedelta(days=days)
        statement = statement.where(Trade.closed_at >= earliest_close)
    records: list[tuple[Trade, Instrument, list[Fill]]] = []
    for trade, instrument in db.execute(statement):
        fills = list(
            db.scalars(
                select(Fill)
                .where(
                    Fill.workspace_id == scope.workspace_id,
                    Fill.account_id == scope.account_id,
                    Fill.trade_id == trade.id,
                )
                .order_by(Fill.occurred_at)
            )
        )
        records.append((trade, instrument, fills))
    return summarize_broker_trades(records, account_currency=account.currency)
