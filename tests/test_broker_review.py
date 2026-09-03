import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.models import BrokerConnection, ExecutionEvent, Fill, Instrument, Trade
from app.services.broker_review import broker_trade_review, summarize_broker_trades


def _fill(
    *,
    side: str,
    quantity: str,
    realized_pnl: str | None = None,
    commission: str | None = None,
):
    return SimpleNamespace(
        side=side,
        quantity=Decimal(quantity),
        realized_pnl=(Decimal(realized_pnl) if realized_pnl is not None else None),
        commission=(Decimal(commission) if commission is not None else None),
        financing=None,
        guaranteed_execution_fee=None,
        half_spread_cost=None,
        ingested_at=datetime(2026, 9, 2, 15, tzinfo=UTC),
    )


def _record(*, held_seconds: int, pnl: str, side: str = "long"):
    opened = datetime(2026, 9, 2, 14, tzinfo=UTC)
    opening_side = "buy" if side == "long" else "sell"
    closing_side = "sell" if side == "long" else "buy"
    trade = SimpleNamespace(
        id=uuid.uuid4(),
        direction=side,
        opened_at=opened,
        closed_at=opened + timedelta(seconds=held_seconds),
    )
    instrument = SimpleNamespace(canonical_symbol="XAU_USD")
    fills = [
        _fill(side=opening_side, quantity="5"),
        _fill(
            side=closing_side,
            quantity="5",
            realized_pnl=pnl,
            commission="-2",
        ),
    ]
    return trade, instrument, fills


def test_summarize_broker_trades_uses_currency_units_net_and_holding_buckets() -> None:
    report = summarize_broker_trades(
        [
            _record(held_seconds=30, pnl="102"),
            _record(held_seconds=600, pnl="-48", side="short"),
            _record(held_seconds=1800, pnl="202"),
        ],
        account_currency="USD",
    )

    assert report.trade_count == 3
    assert report.winners == 2
    assert report.losers == 1
    assert report.net_pnl == Decimal("250")
    assert report.account_currency == "USD"
    assert report.quantity_unit == "broker-reported units (not assumed to be lots)"
    assert [bucket.trade_count for bucket in report.holding_buckets] == [1, 0, 1, 1]
    assert report.trades[0].quantity == Decimal("5")
    assert report.trades[0].net_pnl == Decimal("100")


def test_summarize_broker_trades_does_not_invent_missing_pnl() -> None:
    trade, instrument, fills = _record(held_seconds=90, pnl="10")
    fills[1].realized_pnl = None

    report = summarize_broker_trades(
        [(trade, instrument, fills)],
        account_currency="USD",
    )

    assert report.net_pnl is None
    assert report.unknown_outcomes == 1
    assert report.trades[0].net_pnl is None


def test_broker_trade_review_is_scoped_and_reads_normalized_fills(
    db_session,
    workspace_account,
    request_scope,
) -> None:
    workspace, account = workspace_account
    connection = BrokerConnection(
        workspace_id=workspace.id,
        account_id=account.id,
        provider="metatrader-mt5-bridge",
        environment="practice",
    )
    instrument = Instrument(
        canonical_symbol="XAU_USD",
        asset_class="forex",
    )
    db_session.add_all([connection, instrument])
    db_session.flush()
    opened = datetime(2026, 9, 2, 14, tzinfo=UTC)
    trade = Trade(
        workspace_id=workspace.id,
        account_id=account.id,
        instrument_id=instrument.id,
        external_trade_id=f"trade-{uuid.uuid4()}",
        direction="long",
        status="closed",
        origin="broker_import",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=2),
    )
    db_session.add(trade)
    db_session.flush()
    execution = ExecutionEvent(
        workspace_id=workspace.id,
        account_id=account.id,
        connection_id=connection.id,
        trade_id=trade.id,
        external_event_id=f"event-{uuid.uuid4()}",
        external_trade_id=trade.external_trade_id,
        event_type="deal_fill",
        occurred_at=trade.closed_at,
    )
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        Fill(
            workspace_id=workspace.id,
            account_id=account.id,
            connection_id=connection.id,
            trade_id=trade.id,
            execution_event_id=execution.id,
            instrument_id=instrument.id,
            external_fill_id=f"fill-{uuid.uuid4()}",
            side="buy",
            quantity=Decimal("0.5"),
            price=Decimal("3500"),
            realized_pnl=Decimal("125"),
            commission=Decimal("-2"),
            occurred_at=trade.closed_at,
        )
    )
    db_session.commit()

    report = broker_trade_review(db_session, scope=request_scope, limit=10)

    assert report.trade_count == 1
    assert report.net_pnl == Decimal("123")
    assert report.trades[0].instrument == "XAU_USD"
