import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.market_data.contracts import (
    AccountState,
    BrokerEvent,
    BrokerTradeEffect,
    PositionState,
    SyncPage,
)
from app.models import (
    AccountSnapshot,
    ConnectorCursor,
    ExecutionEvent,
    Fill,
    PositionSnapshot,
    Trade,
)
from app.services.broker_sync import synchronize_broker
from app.services.catalog import configure_account
from app.services.workspaces import RequestScope

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)


class FakeReadOnlyBroker:
    name = "fake-broker"

    async def events_since(self, cursor):
        return SyncPage(
            events=(
                BrokerEvent(
                    external_id="fill-1",
                    event_type="order_fill",
                    occurred_at=NOW,
                    instrument="XAU_USD",
                    external_order_id="order-1",
                    external_trade_id="trade-1",
                    quantity=Decimal("2"),
                    price=Decimal("2400"),
                    realized_pnl=Decimal("0"),
                    source="fake-broker",
                ),
            ),
            cursor_before=cursor,
            cursor_after="1",
            has_more=False,
            coverage="complete",
        )

    async def account(self):
        return AccountState(
            external_account_id="account-1",
            currency="USD",
            balance=Decimal("10000"),
            equity=Decimal("10020"),
            margin_used=Decimal("100"),
            margin_available=Decimal("9920"),
            market_time=NOW,
            retrieved_at=NOW,
            source="fake-broker",
        )

    async def positions(self):
        return (
            PositionState(
                external_id="XAU_USD",
                instrument="XAU_USD",
                net_quantity=Decimal("2"),
                average_price=Decimal("2400"),
                unrealized_pnl=Decimal("20"),
                market_time=NOW,
                retrieved_at=NOW,
                source="fake-broker",
            ),
        )


def test_broker_sync_is_idempotent_and_reconciles(
    db_session,
    workspace_account,
) -> None:
    workspace, _ = workspace_account
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="fake",
        external_account_id="account-1",
        label="test",
        currency="USD",
        mode="practice",
        provider="fake-broker",
        environment="practice",
        config_reference=None,
    )
    request_scope = RequestScope(workspace.id, account.id)
    connector = FakeReadOnlyBroker()

    first = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )
    second = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )

    event_count = db_session.scalar(select(func.count()).select_from(ExecutionEvent))
    fill_count = db_session.scalar(select(func.count()).select_from(Fill))
    assert first.imported_events == 1
    assert first.imported_fills == 1
    assert first.reconciliation_issues == ()
    assert second.imported_events == 0
    assert second.duplicate_events == 1
    assert event_count == 1
    assert fill_count == 1
    assert connection.status == "healthy"
    assert connection.last_healthy_at is not None


class LifecycleReadOnlyBroker(FakeReadOnlyBroker):
    async def events_since(self, cursor):
        def event(
            external_id: str,
            *,
            minutes: int,
            quantity: str,
            trade_effect: BrokerTradeEffect,
            realized_pnl: str = "0",
            commission: str = "0",
            financing: str = "0",
            guaranteed_execution_fee: str = "0",
            half_spread_cost: str = "0",
        ) -> BrokerEvent:
            return BrokerEvent(
                external_id=external_id,
                event_type="order_fill",
                occurred_at=NOW.replace(minute=minutes),
                instrument="XAU_USD",
                external_order_id=f"order-{external_id}",
                external_trade_id=trade_effect.external_trade_id,
                quantity=Decimal(quantity),
                price=Decimal("2400"),
                realized_pnl=Decimal(realized_pnl),
                source=self.name,
                commission=Decimal(commission),
                financing=Decimal(financing),
                guaranteed_execution_fee=Decimal(guaranteed_execution_fee),
                half_spread_cost=Decimal(half_spread_cost),
                trade_effects=(trade_effect,),
            )

        return SyncPage(
            events=(
                event(
                    "fill-open-long",
                    minutes=0,
                    quantity="2",
                    trade_effect=BrokerTradeEffect(
                        "trade-long",
                        "opened",
                        Decimal("2"),
                    ),
                ),
                event(
                    "fill-reduce-long",
                    minutes=1,
                    quantity="-1",
                    trade_effect=BrokerTradeEffect(
                        "trade-long",
                        "reduced",
                        Decimal("1"),
                        Decimal("25"),
                    ),
                    realized_pnl="25",
                    commission="-0.50",
                    financing="-0.25",
                    half_spread_cost="0.40",
                ),
                event(
                    "fill-close-long",
                    minutes=2,
                    quantity="-1",
                    trade_effect=BrokerTradeEffect(
                        "trade-long",
                        "closed",
                        Decimal("1"),
                        Decimal("40"),
                    ),
                    realized_pnl="40",
                    commission="-0.50",
                    guaranteed_execution_fee="-0.10",
                    half_spread_cost="0.40",
                ),
                event(
                    "fill-open-short",
                    minutes=3,
                    quantity="-1",
                    trade_effect=BrokerTradeEffect(
                        "trade-short",
                        "opened",
                        Decimal("-1"),
                    ),
                ),
            ),
            cursor_before=cursor,
            cursor_after="4",
            has_more=False,
            coverage="complete",
        )

    async def positions(self):
        return (
            PositionState(
                external_id="XAU_USD",
                instrument="XAU_USD",
                net_quantity=Decimal("-1"),
                average_price=Decimal("2400"),
                unrealized_pnl=Decimal("10"),
                market_time=NOW.replace(minute=3),
                retrieved_at=NOW.replace(minute=3),
                source=self.name,
            ),
        )


def test_broker_sync_tracks_lifecycle_costs_and_snapshot_links(
    db_session,
    workspace_account,
) -> None:
    workspace, _ = workspace_account
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="fake",
        external_account_id="account-1",
        label="lifecycle",
        currency="USD",
        mode="practice",
        provider="fake-broker",
        environment="practice",
        config_reference=None,
    )
    request_scope = RequestScope(workspace.id, account.id)
    connector = LifecycleReadOnlyBroker()

    first = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )
    second = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )

    trades = {
        item.external_trade_id: item
        for item in db_session.scalars(select(Trade).order_by(Trade.opened_at))
    }
    closed = trades["trade-long"]
    active = trades["trade-short"]
    close_fill = db_session.scalar(
        select(Fill).where(Fill.external_fill_id == "fill-close-long")
    )
    close_event = db_session.scalar(
        select(ExecutionEvent).where(
            ExecutionEvent.external_event_id == "fill-close-long"
        )
    )
    latest_position = db_session.scalar(
        select(PositionSnapshot).order_by(PositionSnapshot.retrieved_at.desc())
    )
    latest_account = db_session.scalar(
        select(AccountSnapshot).order_by(AccountSnapshot.retrieved_at.desc())
    )

    assert first.imported_events == 4
    assert first.imported_fills == 4
    assert first.reconciliation_issues == ()
    assert second.imported_events == 0
    assert second.duplicate_events == 4
    assert db_session.scalar(select(func.count()).select_from(Trade)) == 2
    assert db_session.scalar(select(func.count()).select_from(Fill)) == 4
    assert closed.status == "closed"
    assert closed.closed_at == NOW.replace(minute=2)
    assert active.status == "open"
    assert active.direction == "short"
    assert close_fill is not None
    assert close_fill.trade_id == closed.id
    assert close_fill.commission == Decimal("-0.5000")
    assert close_fill.financing == Decimal("0.0000")
    assert close_fill.guaranteed_execution_fee == Decimal("-0.1000")
    assert close_fill.half_spread_cost == Decimal("0.4000")
    assert close_event is not None
    assert close_event.provider_metadata["trade_effects"] == [
        {
            "external_trade_id": "trade-long",
            "effect": "closed",
            "quantity": "1",
            "realized_pnl": "40",
        }
    ]
    assert latest_position is not None
    assert latest_position.trade_id == active.id
    assert latest_account is not None
    assert latest_account.execution_event_id is not None


class MissingHistoryReadOnlyBroker(FakeReadOnlyBroker):
    async def events_since(self, cursor):
        return SyncPage(
            events=(
                BrokerEvent(
                    external_id="late-close",
                    event_type="order_fill",
                    occurred_at=NOW,
                    instrument="XAU_USD",
                    external_order_id="late-order",
                    external_trade_id="unknown-trade",
                    quantity=Decimal("-1"),
                    price=Decimal("2400"),
                    realized_pnl=Decimal("5"),
                    source=self.name,
                    trade_effects=(
                        BrokerTradeEffect(
                            "unknown-trade",
                            "closed",
                            Decimal("1"),
                            Decimal("5"),
                        ),
                    ),
                ),
            ),
            cursor_before=cursor,
            cursor_after="1",
            has_more=False,
            coverage="incremental",
        )

    async def positions(self):
        return ()


def test_broker_sync_does_not_invent_missing_trade_open_history(
    db_session,
    workspace_account,
) -> None:
    workspace, _ = workspace_account
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="fake",
        external_account_id="account-1",
        label="partial-history",
        currency="USD",
        mode="practice",
        provider="fake-broker",
        environment="practice",
        config_reference=None,
    )
    request_scope = RequestScope(workspace.id, account.id)

    result = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=MissingHistoryReadOnlyBroker(),
            scope=request_scope,
        )
    )

    assert result.imported_events == 1
    assert result.imported_fills == 1
    assert db_session.scalar(select(func.count()).select_from(Trade)) == 0
    execution = db_session.scalar(select(ExecutionEvent))
    assert execution is not None
    assert execution.trade_id is None
    assert execution.provider_metadata["trade_effects"][0]["effect"] == "closed"


class NonInferableHistoryBroker(FakeReadOnlyBroker):
    async def events_since(self, cursor):
        return SyncPage(
            events=(
                BrokerEvent(
                    external_id="ambiguous-exit",
                    event_type="deal_fill",
                    occurred_at=NOW,
                    instrument="XAU_USD",
                    external_order_id="order-ambiguous",
                    external_trade_id="position-unknown",
                    quantity=Decimal("-1"),
                    price=Decimal("2400"),
                    realized_pnl=Decimal("5"),
                    source=self.name,
                    infer_trade_open=False,
                ),
            ),
            cursor_before=cursor,
            cursor_after="1",
            has_more=False,
            coverage="incremental",
        )

    async def positions(self):
        return ()


def test_broker_sync_preserves_ambiguous_external_trade_without_inventing_lifecycle(
    db_session,
    workspace_account,
) -> None:
    workspace, _ = workspace_account
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="fake",
        external_account_id="account-1",
        label="ambiguous-history",
        currency="USD",
        mode="practice",
        provider="fake-broker",
        environment="practice",
        config_reference=None,
    )
    request_scope = RequestScope(workspace.id, account.id)

    result = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=NonInferableHistoryBroker(),
            scope=request_scope,
        )
    )

    assert result.imported_events == 1
    assert result.imported_fills == 1
    assert db_session.scalar(select(func.count()).select_from(Trade)) == 0
    execution = db_session.scalar(select(ExecutionEvent))
    assert execution is not None
    assert execution.external_trade_id == "position-unknown"
    assert execution.trade_id is None


class BaselinePositionBroker(FakeReadOnlyBroker):
    async def events_since(self, cursor):
        return SyncPage(
            events=(),
            cursor_before=cursor,
            cursor_after="baseline-1",
            has_more=False,
            coverage="baseline" if cursor is None else "incremental",
        )


def test_broker_sync_does_not_reconcile_partial_history_as_a_full_ledger(
    db_session,
    workspace_account,
) -> None:
    workspace, _ = workspace_account
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="fake",
        external_account_id="account-1",
        label="baseline-position",
        currency="USD",
        mode="practice",
        provider="fake-broker",
        environment="practice",
        config_reference=None,
    )
    request_scope = RequestScope(workspace.id, account.id)
    connector = BaselinePositionBroker()

    baseline = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )
    incremental = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )

    assert baseline.coverage == "baseline"
    assert baseline.reconciliation_performed is False
    assert baseline.reconciliation_issues == ()
    assert baseline.cursor_before is None
    assert baseline.cursor_after == "baseline-1"
    assert incremental.coverage == "incremental"
    assert incremental.reconciliation_performed is False
    assert incremental.reconciliation_issues == ()
    assert connection.status == "configured"
    assert connection.last_healthy_at is None


class ConflictingEventBroker(FakeReadOnlyBroker):
    async def events_since(self, cursor):
        event = BrokerEvent(
            external_id="fill-1",
            event_type="order_fill",
            occurred_at=NOW,
            instrument="XAU_USD",
            external_order_id="order-1",
            external_trade_id="trade-1",
            quantity=Decimal("2"),
            price=Decimal("2400") if cursor is None else Decimal("2401"),
            realized_pnl=Decimal("0"),
            source=self.name,
        )
        return SyncPage(
            events=(event,),
            cursor_before=cursor,
            cursor_after="1" if cursor is None else "2",
            has_more=cursor is None,
            coverage="complete",
        )


def test_broker_sync_surfaces_conflicting_external_event_ids_and_holds_cursor(
    db_session,
    workspace_account,
) -> None:
    workspace, _ = workspace_account
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="fake",
        external_account_id="account-1",
        label="conflicting-event",
        currency="USD",
        mode="practice",
        provider="fake-broker",
        environment="practice",
        config_reference=None,
    )
    request_scope = RequestScope(workspace.id, account.id)
    connector = ConflictingEventBroker()

    first = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )
    second = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
            scope=request_scope,
        )
    )

    cursor = db_session.scalar(
        select(ConnectorCursor).where(
            ConnectorCursor.connection_id == connection.id,
            ConnectorCursor.stream_name == "transactions",
        )
    )
    assert first.conflicting_events == 0
    assert first.has_more is True
    assert second.duplicate_events == 0
    assert second.conflicting_events == 1
    assert second.cursor_before == "1"
    assert second.cursor_after == "2"
    assert db_session.scalar(select(func.count()).select_from(ExecutionEvent)) == 1
    assert db_session.scalar(select(func.count()).select_from(Fill)) == 1
    assert cursor is not None
    assert cursor.cursor_value == "1"
    assert connection.status == "degraded"
