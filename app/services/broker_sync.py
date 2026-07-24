import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.market_data.contracts import BrokerEvent, ReadOnlyBrokerConnector
from app.models import (
    AccountSnapshot,
    BrokerConnection,
    ConnectorCursor,
    ExecutionEvent,
    Fill,
    InstrumentMapping,
    PositionSnapshot,
    Trade,
)
from app.services.catalog import get_or_create_instrument, get_or_create_mapping


@dataclass(frozen=True)
class ReconciliationIssue:
    instrument: str
    broker_quantity: Decimal
    ledger_quantity: Decimal


@dataclass(frozen=True)
class BrokerSyncResult:
    imported_events: int
    imported_fills: int
    duplicate_events: int
    cursor: str | None
    reconciliation_issues: tuple[ReconciliationIssue, ...]


def _event_hash(event: BrokerEvent) -> str:
    payload = {
        "external_id": event.external_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.isoformat(),
        "instrument": event.instrument,
        "external_order_id": event.external_order_id,
        "external_trade_id": event.external_trade_id,
        "quantity": str(event.quantity) if event.quantity is not None else None,
        "price": str(event.price) if event.price is not None else None,
        "realized_pnl": (
            str(event.realized_pnl) if event.realized_pnl is not None else None
        ),
        "source": event.source,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def _ledger_quantities(db: Session, connection_id: uuid.UUID) -> dict[uuid.UUID, Decimal]:
    totals: dict[uuid.UUID, Decimal] = {}
    fills = db.scalars(select(Fill).where(Fill.connection_id == connection_id))
    for fill in fills:
        signed = fill.quantity if fill.side == "buy" else -fill.quantity
        totals[fill.instrument_id] = totals.get(fill.instrument_id, Decimal("0")) + signed
    return totals


async def synchronize_broker(
    db: Session,
    *,
    connection_id: uuid.UUID,
    connector: ReadOnlyBrokerConnector,
) -> BrokerSyncResult:
    connection = db.get(BrokerConnection, connection_id)
    if connection is None:
        raise LookupError("broker connection not found")
    cursor_record = db.scalar(
        select(ConnectorCursor).where(
            ConnectorCursor.connection_id == connection.id,
            ConnectorCursor.stream_name == "transactions",
        )
    )
    cursor = cursor_record.cursor_value if cursor_record else None
    events, next_cursor = await connector.events_since(cursor)
    imported_events = 0
    imported_fills = 0
    duplicate_events = 0

    for event in events:
        existing = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.connection_id == connection.id,
                ExecutionEvent.external_event_id == event.external_id,
            )
        )
        if existing is not None:
            duplicate_events += 1
            continue
        instrument = None
        mapping = None
        trade = None
        if event.instrument is not None:
            instrument = get_or_create_instrument(db, event.instrument)
            mapping = get_or_create_mapping(
                db,
                instrument,
                provider=connection.provider,
                external_symbol=event.instrument,
                venue=connector.name,
            )
        if event.external_trade_id is not None and instrument is not None:
            trade = db.scalar(
                select(Trade).where(
                    Trade.account_id == connection.account_id,
                    Trade.external_trade_id == event.external_trade_id,
                )
            )
            if trade is None and event.quantity is not None:
                trade = Trade(
                    account_id=connection.account_id,
                    instrument_id=instrument.id,
                    external_trade_id=event.external_trade_id,
                    direction="long" if event.quantity > 0 else "short",
                    status="open",
                    origin="broker_import",
                    opened_at=event.occurred_at,
                )
                db.add(trade)
                db.flush()
        execution = ExecutionEvent(
            connection_id=connection.id,
            trade_id=trade.id if trade else None,
            external_event_id=event.external_id,
            external_order_id=event.external_order_id,
            external_trade_id=event.external_trade_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            source_payload_hash=_event_hash(event),
            provider_metadata={"normalized_source": event.source},
        )
        db.add(execution)
        db.flush()
        imported_events += 1
        if (
            "fill" in event.event_type
            and instrument is not None
            and mapping is not None
            and event.quantity is not None
            and event.price is not None
        ):
            fill = Fill(
                connection_id=connection.id,
                trade_id=trade.id if trade else None,
                execution_event_id=execution.id,
                instrument_id=instrument.id,
                external_fill_id=event.external_id,
                side="buy" if event.quantity > 0 else "sell",
                quantity=abs(event.quantity),
                price=event.price,
                realized_pnl=event.realized_pnl,
                occurred_at=event.occurred_at,
            )
            db.add(fill)
            imported_fills += 1

    if next_cursor is not None:
        if cursor_record is None:
            cursor_record = ConnectorCursor(
                connection_id=connection.id,
                stream_name="transactions",
                cursor_value=next_cursor,
            )
            db.add(cursor_record)
        else:
            cursor_record.cursor_value = next_cursor

    account_state = await connector.account()
    if account_state.external_account_id != connection.account.external_account_id:
        raise ValueError("connector account does not match the configured account")
    db.add(
        AccountSnapshot(
            account_id=connection.account_id,
            trigger="reconciliation",
            currency=account_state.currency,
            balance=account_state.balance,
            equity=account_state.equity,
            margin_used=account_state.margin_used,
            margin_available=account_state.margin_available,
            market_time=account_state.market_time,
            retrieved_at=account_state.retrieved_at,
            source=account_state.source,
        )
    )

    broker_positions = await connector.positions()
    ledger = _ledger_quantities(db, connection.id)
    issues = []
    seen_instruments = set()
    for position in broker_positions:
        instrument = get_or_create_instrument(db, position.instrument)
        get_or_create_mapping(
            db,
            instrument,
            provider=connection.provider,
            external_symbol=position.instrument,
            venue=connector.name,
        )
        seen_instruments.add(instrument.id)
        db.add(
            PositionSnapshot(
                account_id=connection.account_id,
                instrument_id=instrument.id,
                trigger="reconciliation",
                net_quantity=position.net_quantity,
                average_price=position.average_price,
                unrealized_pnl=position.unrealized_pnl,
                market_time=position.market_time,
                retrieved_at=position.retrieved_at,
                source=position.source,
            )
        )
        ledger_quantity = ledger.get(instrument.id, Decimal("0"))
        if ledger_quantity != position.net_quantity:
            issues.append(
                ReconciliationIssue(
                    instrument=instrument.canonical_symbol,
                    broker_quantity=position.net_quantity,
                    ledger_quantity=ledger_quantity,
                )
            )
    for instrument_id, ledger_quantity in ledger.items():
        if instrument_id not in seen_instruments and ledger_quantity != 0:
            mapping = db.scalar(
                select(InstrumentMapping).where(
                    InstrumentMapping.instrument_id == instrument_id,
                    InstrumentMapping.provider == connection.provider,
                )
            )
            issues.append(
                ReconciliationIssue(
                    instrument=mapping.external_symbol if mapping else str(instrument_id),
                    broker_quantity=Decimal("0"),
                    ledger_quantity=ledger_quantity,
                )
            )
    connection.status = "degraded" if issues else "healthy"
    if not issues:
        connection.last_healthy_at = datetime.now(UTC)
    db.commit()
    return BrokerSyncResult(
        imported_events=imported_events,
        imported_fills=imported_fills,
        duplicate_events=duplicate_events,
        cursor=next_cursor,
        reconciliation_issues=tuple(issues),
    )
