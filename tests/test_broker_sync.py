import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.market_data.contracts import (
    AccountState,
    BrokerEvent,
    PositionState,
)
from app.models import ExecutionEvent, Fill
from app.services.broker_sync import synchronize_broker
from app.services.catalog import configure_account

NOW = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)


class FakeReadOnlyBroker:
    name = "fake-broker"

    async def events_since(self, cursor):
        return (
            (
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
            "1",
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


def test_broker_sync_is_idempotent_and_reconciles(db_session) -> None:
    _, connection = configure_account(
        db_session,
        broker="fake",
        external_account_id="account-1",
        label="test",
        currency="USD",
        mode="practice",
        provider="fake-broker",
        environment="practice",
        config_reference=None,
    )
    connector = FakeReadOnlyBroker()

    first = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
        )
    )
    second = asyncio.run(
        synchronize_broker(
            db_session,
            connection_id=connection.id,
            connector=connector,
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
