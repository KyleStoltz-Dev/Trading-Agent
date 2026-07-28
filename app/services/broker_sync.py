import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.market_data.contracts import (
    BrokerEvent,
    BrokerTradeEffect,
    ReadOnlyBrokerConnector,
    SyncCoverage,
)
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
from app.services.workspaces import RequestScope, validate_scope


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
    conflicting_events: int
    cursor_before: str | None
    cursor_after: str | None
    has_more: bool
    coverage: SyncCoverage
    reconciliation_performed: bool
    reconciliation_issues: tuple[ReconciliationIssue, ...]


class BrokerSyncInProgressError(RuntimeError):
    pass


class BrokerSyncConflictError(RuntimeError):
    pass


def _sync_lock_key(scope: RequestScope, connection_id: uuid.UUID) -> int:
    digest = hashlib.sha256(
        (
            f"trading-agent:broker-sync:{scope.workspace_id}:"
            f"{scope.account_id}:{connection_id}"
        ).encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@contextmanager
def _broker_sync_lock(
    db: Session,
    *,
    scope: RequestScope,
    connection_id: uuid.UUID,
) -> Iterator[None]:
    """Serialize one account feed without holding the ingestion transaction open."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        yield
        return
    connection = bind.engine.connect()
    key = _sync_lock_key(scope, connection_id)
    try:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": key},
            )
        )
        connection.commit()
        if not acquired:
            raise BrokerSyncInProgressError(
                "a broker synchronization is already running for this account"
            )
        yield
    finally:
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": key},
            )
            connection.commit()
        finally:
            connection.close()


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
        "commission": str(event.commission) if event.commission is not None else None,
        "financing": str(event.financing) if event.financing is not None else None,
        "guaranteed_execution_fee": (
            str(event.guaranteed_execution_fee)
            if event.guaranteed_execution_fee is not None
            else None
        ),
        "half_spread_cost": (
            str(event.half_spread_cost)
            if event.half_spread_cost is not None
            else None
        ),
        "trade_effects": [
            {
                "external_trade_id": effect.external_trade_id,
                "effect": effect.effect,
                "quantity": str(effect.quantity),
                "realized_pnl": (
                    str(effect.realized_pnl)
                    if effect.realized_pnl is not None
                    else None
                ),
            }
            for effect in event.trade_effects
        ],
        "infer_trade_open": event.infer_trade_open,
        "source": event.source,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def _ledger_quantities(
    db: Session,
    connection_id: uuid.UUID,
    *,
    scope: RequestScope,
) -> dict[uuid.UUID, Decimal]:
    totals: dict[uuid.UUID, Decimal] = {}
    fills = db.scalars(
        select(Fill).where(
            Fill.workspace_id == scope.workspace_id,
            Fill.account_id == scope.account_id,
            Fill.connection_id == connection_id,
        )
    )
    for fill in fills:
        signed = fill.quantity if fill.side == "buy" else -fill.quantity
        totals[fill.instrument_id] = totals.get(fill.instrument_id, Decimal("0")) + signed
    return totals


def _trade_effect_metadata(effect: BrokerTradeEffect) -> dict[str, str | None]:
    return {
        "external_trade_id": effect.external_trade_id,
        "effect": effect.effect,
        "quantity": str(effect.quantity),
        "realized_pnl": (
            str(effect.realized_pnl) if effect.realized_pnl is not None else None
        ),
    }


def _find_lifecycle_trade(
    db: Session,
    *,
    scope: RequestScope,
    external_trade_id: str,
) -> Trade | None:
    return db.scalar(
        select(Trade).where(
            Trade.workspace_id == scope.workspace_id,
            Trade.account_id == scope.account_id,
            Trade.external_trade_id == external_trade_id,
        )
    )


def _apply_trade_effects(
    db: Session,
    *,
    connection: BrokerConnection,
    event: BrokerEvent,
    instrument_id: uuid.UUID | None,
) -> dict[str, Trade]:
    """Apply explicit provider lifecycle effects without inferring missing history."""
    trades: dict[str, Trade] = {}
    for effect in event.trade_effects:
        trade = _find_lifecycle_trade(
            db,
            scope=RequestScope(
                workspace_id=connection.workspace_id,
                account_id=connection.account_id,
            ),
            external_trade_id=effect.external_trade_id,
        )
        if effect.effect == "opened":
            if instrument_id is None:
                continue
            if trade is None:
                trade = Trade(
                    workspace_id=connection.workspace_id,
                    account_id=connection.account_id,
                    instrument_id=instrument_id,
                    external_trade_id=effect.external_trade_id,
                    direction="long" if effect.quantity > 0 else "short",
                    status="open",
                    origin="broker_import",
                    opened_at=event.occurred_at,
                )
                db.add(trade)
                db.flush()
            else:
                trade.status = "open"
                trade.closed_at = None
                trade.opened_at = trade.opened_at or event.occurred_at
        elif trade is None:
            # A history cursor may begin after the opening transaction. Preserve the
            # normalized effect on ExecutionEvent instead of inventing an opening trade.
            continue
        elif effect.effect == "reduced":
            trade.status = "partially_closed"
        elif effect.effect == "closed":
            trade.status = "closed"
            trade.closed_at = event.occurred_at
        trades[effect.external_trade_id] = trade
    return trades


def _primary_trade(
    event: BrokerEvent,
    trades: dict[str, Trade],
) -> Trade | None:
    for effect_name in ("opened", "reduced", "closed"):
        for effect in event.trade_effects:
            if effect.effect == effect_name and effect.external_trade_id in trades:
                return trades[effect.external_trade_id]
    return next(iter(trades.values()), None)


def _single_active_trade(
    db: Session,
    *,
    scope: RequestScope,
    instrument_id: uuid.UUID,
) -> Trade | None:
    candidates = list(
        db.scalars(
            select(Trade)
            .where(
                Trade.workspace_id == scope.workspace_id,
                Trade.account_id == scope.account_id,
                Trade.instrument_id == instrument_id,
                Trade.status.in_(("open", "partially_closed")),
            )
            .order_by(Trade.opened_at.desc().nullslast(), Trade.created_at.desc())
            .limit(2)
        )
    )
    return candidates[0] if len(candidates) == 1 else None


async def synchronize_broker(
    db: Session,
    *,
    scope: RequestScope,
    connection_id: uuid.UUID,
    connector: ReadOnlyBrokerConnector,
) -> BrokerSyncResult:
    with _broker_sync_lock(db, scope=scope, connection_id=connection_id):
        return await _synchronize_broker_locked(
            db,
            scope=scope,
            connection_id=connection_id,
            connector=connector,
        )


async def _synchronize_broker_locked(
    db: Session,
    *,
    scope: RequestScope,
    connection_id: uuid.UUID,
    connector: ReadOnlyBrokerConnector,
) -> BrokerSyncResult:
    validate_scope(db, scope)
    connection = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.workspace_id == scope.workspace_id,
            BrokerConnection.account_id == scope.account_id,
            BrokerConnection.id == connection_id,
        )
    )
    if connection is None:
        raise LookupError("broker connection not found")
    expected_external_account_id = connection.account.external_account_id
    cursor_record = db.scalar(
        select(ConnectorCursor).where(
            ConnectorCursor.workspace_id == scope.workspace_id,
            ConnectorCursor.account_id == scope.account_id,
            ConnectorCursor.connection_id == connection.id,
            ConnectorCursor.stream_name == "transactions",
        )
    )
    cursor = cursor_record.cursor_value if cursor_record else None
    db.commit()

    page = await connector.events_since(cursor)
    account_state = await connector.account()
    broker_positions = await connector.positions()
    if page.cursor_before != cursor:
        raise ValueError("connector sync page cursor does not match the requested cursor")
    if account_state.external_account_id != expected_external_account_id:
        raise ValueError("connector account does not match the configured account")

    connection = db.scalar(
        select(BrokerConnection)
        .where(
            BrokerConnection.workspace_id == scope.workspace_id,
            BrokerConnection.account_id == scope.account_id,
            BrokerConnection.id == connection_id,
        )
        .with_for_update()
    )
    if connection is None:
        raise LookupError("broker connection not found")
    cursor_record = db.scalar(
        select(ConnectorCursor)
        .where(
            ConnectorCursor.workspace_id == scope.workspace_id,
            ConnectorCursor.account_id == scope.account_id,
            ConnectorCursor.connection_id == connection.id,
            ConnectorCursor.stream_name == "transactions",
        )
        .with_for_update()
    )
    current_cursor = cursor_record.cursor_value if cursor_record else None
    if current_cursor != cursor:
        db.rollback()
        raise BrokerSyncConflictError(
            "broker cursor changed while data was fetched; retry synchronization"
        )
    imported_events = 0
    imported_fills = 0
    duplicate_events = 0
    conflicting_events = 0
    latest_execution: ExecutionEvent | None = None

    for event in page.events:
        event_hash = _event_hash(event)
        existing = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.workspace_id == scope.workspace_id,
                ExecutionEvent.account_id == scope.account_id,
                ExecutionEvent.connection_id == connection.id,
                ExecutionEvent.external_event_id == event.external_id,
            )
        )
        if existing is not None:
            if existing.source_payload_hash == event_hash:
                duplicate_events += 1
                if (
                    latest_execution is None
                    or existing.occurred_at > latest_execution.occurred_at
                ):
                    latest_execution = existing
            else:
                conflicting_events += 1
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
        lifecycle_trades = _apply_trade_effects(
            db,
            connection=connection,
            event=event,
            instrument_id=instrument.id if instrument else None,
        )
        trade = _primary_trade(event, lifecycle_trades)
        if (
            not event.trade_effects
            and event.infer_trade_open
            and event.external_trade_id is not None
            and instrument is not None
        ):
            trade = _find_lifecycle_trade(
                db,
                scope=scope,
                external_trade_id=event.external_trade_id,
            )
            if trade is None and event.quantity is not None and event.quantity != 0:
                trade = Trade(
                    workspace_id=scope.workspace_id,
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
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            connection_id=connection.id,
            trade_id=trade.id if trade else None,
            external_event_id=event.external_id,
            external_order_id=event.external_order_id,
            external_trade_id=event.external_trade_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            source_payload_hash=event_hash,
            provider_metadata={
                "normalized_source": event.source,
                "trade_effects": [
                    _trade_effect_metadata(effect)
                    for effect in event.trade_effects
                ],
                "costs": {
                    "commission": (
                        str(event.commission)
                        if event.commission is not None
                        else None
                    ),
                    "financing": (
                        str(event.financing)
                        if event.financing is not None
                        else None
                    ),
                    "guaranteed_execution_fee": (
                        str(event.guaranteed_execution_fee)
                        if event.guaranteed_execution_fee is not None
                        else None
                    ),
                    "half_spread_cost": (
                        str(event.half_spread_cost)
                        if event.half_spread_cost is not None
                        else None
                    ),
                },
            },
        )
        db.add(execution)
        db.flush()
        if (
            latest_execution is None
            or execution.occurred_at > latest_execution.occurred_at
        ):
            latest_execution = execution
        imported_events += 1
        if (
            "fill" in event.event_type
            and instrument is not None
            and mapping is not None
            and event.quantity is not None
            and event.quantity != 0
            and event.price is not None
        ):
            fill = Fill(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                connection_id=connection.id,
                trade_id=trade.id if trade else None,
                execution_event_id=execution.id,
                instrument_id=instrument.id,
                external_fill_id=event.external_id,
                side="buy" if event.quantity > 0 else "sell",
                quantity=abs(event.quantity),
                price=event.price,
                commission=event.commission,
                financing=event.financing,
                guaranteed_execution_fee=event.guaranteed_execution_fee,
                half_spread_cost=event.half_spread_cost,
                realized_pnl=event.realized_pnl,
                occurred_at=event.occurred_at,
            )
            db.add(fill)
            imported_fills += 1

    if page.cursor_after is not None and not conflicting_events:
        if cursor_record is None:
            cursor_record = ConnectorCursor(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                connection_id=connection.id,
                stream_name="transactions",
                cursor_value=page.cursor_after,
            )
            db.add(cursor_record)
        else:
            cursor_record.cursor_value = page.cursor_after

    db.add(
        AccountSnapshot(
            workspace_id=scope.workspace_id,
            account_id=connection.account_id,
            execution_event_id=latest_execution.id if latest_execution else None,
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

    reconciliation_performed = page.coverage == "complete"
    ledger = (
        _ledger_quantities(db, connection.id, scope=scope)
        if reconciliation_performed
        else {}
    )
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
        lifecycle_trade = _single_active_trade(
            db,
            scope=scope,
            instrument_id=instrument.id,
        )
        db.add(
            PositionSnapshot(
                workspace_id=scope.workspace_id,
                account_id=connection.account_id,
                instrument_id=instrument.id,
                trade_id=lifecycle_trade.id if lifecycle_trade else None,
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
        if reconciliation_performed and ledger_quantity != position.net_quantity:
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
    if issues or conflicting_events:
        connection.status = "degraded"
    elif reconciliation_performed:
        connection.status = "healthy"
        connection.last_healthy_at = datetime.now(UTC)
    else:
        connection.status = "configured"
    db.commit()
    return BrokerSyncResult(
        imported_events=imported_events,
        imported_fills=imported_fills,
        duplicate_events=duplicate_events,
        conflicting_events=conflicting_events,
        cursor_before=page.cursor_before,
        cursor_after=page.cursor_after,
        has_more=page.has_more,
        coverage=page.coverage,
        reconciliation_performed=reconciliation_performed,
        reconciliation_issues=tuple(issues),
    )
