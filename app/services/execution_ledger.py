import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import OrderApproval, OrderIntent, Trade, TradeManagementEvent, TradePlan
from app.schemas import ManagementEventCreate
from app.services.workspaces import RequestScope, validate_scope


class InvalidIntentTransition(ValueError):
    pass


def record_management_event(
    db: Session,
    trade_id: uuid.UUID,
    request: ManagementEventCreate,
    *,
    scope: RequestScope | None = None,
) -> TradeManagementEvent:
    if scope is None:
        legacy_trade = db.get(Trade, trade_id)
        if legacy_trade is None:
            raise LookupError("trade lifecycle was not found")
        scope = RequestScope(
            workspace_id=legacy_trade.workspace_id,
            account_id=legacy_trade.account_id,
        )
    validate_scope(db, scope)
    trade = db.scalar(
        select(Trade).where(
            Trade.workspace_id == scope.workspace_id,
            Trade.account_id == scope.account_id,
            Trade.id == trade_id,
        )
    )
    if trade is None:
        raise LookupError("trade lifecycle was not found in the requested account")
    event = TradeManagementEvent(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        trade_id=trade.id,
        **request.model_dump(),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _intent_values(intent: OrderIntent) -> dict[str, str | None]:
    return {
        "workspace_id": str(intent.workspace_id),
        "account_id": str(intent.account_id),
        "intent_id": str(intent.id),
        "trade_id": str(intent.trade_id) if intent.trade_id else None,
        "trade_plan_id": str(intent.trade_plan_id) if intent.trade_plan_id else None,
        "action": intent.action,
        "side": intent.side,
        "order_type": intent.order_type,
        "quantity": str(intent.quantity),
        "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
        "stop_price": str(intent.stop_price) if intent.stop_price is not None else None,
        "target_price": (
            str(intent.target_price) if intent.target_price is not None else None
        ),
        "time_in_force": intent.time_in_force,
        "rationale": intent.rationale,
        "policy_hash": intent.policy_hash,
        "proposed_by": intent.proposed_by,
        "idempotency_key": intent.idempotency_key,
        "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
    }


def intent_hash(intent: OrderIntent) -> str:
    serialized = json.dumps(_intent_values(intent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _require_matching_idempotent_intent(
    existing: OrderIntent,
    requested_values: dict[str, object],
) -> OrderIntent:
    if any(
        getattr(existing, field) != value
        for field, value in requested_values.items()
    ):
        raise InvalidIntentTransition(
            "idempotency key was already used for a different order intent"
        )
    return existing


def propose_order_intent(
    db: Session,
    *,
    scope: RequestScope | None = None,
    action: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    rationale: str,
    policy_hash: str,
    proposed_by: str,
    idempotency_key: str,
    trade_id: uuid.UUID | None = None,
    trade_plan_id: uuid.UUID | None = None,
    limit_price: Decimal | None = None,
    stop_price: Decimal | None = None,
    target_price: Decimal | None = None,
    time_in_force: str | None = None,
    expires_at: datetime | None = None,
) -> OrderIntent:
    if scope is None:
        related = (
            db.get(Trade, trade_id)
            if trade_id is not None
            else db.get(TradePlan, trade_plan_id)
            if trade_plan_id is not None
            else None
        )
        if related is None:
            raise ValueError("legacy order intent requires a trade or trade plan")
        scope = RequestScope(
            workspace_id=related.workspace_id,
            account_id=related.account_id,
        )
    validate_scope(db, scope)
    if expires_at is not None and (
        expires_at.tzinfo is None or expires_at.utcoffset() is None
    ):
        raise ValueError("expires_at must be timezone-aware")
    if trade_id is not None:
        trade = db.scalar(
            select(Trade).where(
                Trade.workspace_id == scope.workspace_id,
                Trade.account_id == scope.account_id,
                Trade.id == trade_id,
            )
        )
        if trade is None:
            raise LookupError("trade lifecycle was not found in the requested account")
    if trade_plan_id is not None:
        plan = db.scalar(
            select(TradePlan).where(
                TradePlan.workspace_id == scope.workspace_id,
                TradePlan.account_id == scope.account_id,
                TradePlan.id == trade_plan_id,
            )
        )
        if plan is None:
            raise LookupError("trade plan was not found in the requested account")
        if trade_id is not None and plan.trade_id not in {None, trade_id}:
            raise ValueError("trade plan belongs to a different trade lifecycle")
    requested_values = {
        "trade_id": trade_id,
        "trade_plan_id": trade_plan_id,
        "action": action,
        "side": side,
        "order_type": order_type,
        "quantity": Decimal(quantity),
        "limit_price": None if limit_price is None else Decimal(limit_price),
        "stop_price": None if stop_price is None else Decimal(stop_price),
        "target_price": None if target_price is None else Decimal(target_price),
        "time_in_force": time_in_force,
        "rationale": rationale,
        "policy_hash": policy_hash,
        "proposed_by": proposed_by,
        "expires_at": expires_at,
    }
    existing = db.scalar(
        select(OrderIntent).where(
            OrderIntent.workspace_id == scope.workspace_id,
            OrderIntent.account_id == scope.account_id,
            OrderIntent.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return _require_matching_idempotent_intent(existing, requested_values)
    intent = OrderIntent(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        trade_id=trade_id,
        trade_plan_id=trade_plan_id,
        action=action,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        stop_price=stop_price,
        target_price=target_price,
        time_in_force=time_in_force,
        rationale=rationale,
        policy_hash=policy_hash,
        proposed_by=proposed_by,
        idempotency_key=idempotency_key,
        expires_at=expires_at,
    )
    try:
        with db.begin_nested():
            db.add(intent)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(OrderIntent).where(
                OrderIntent.workspace_id == scope.workspace_id,
                OrderIntent.account_id == scope.account_id,
                OrderIntent.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        return _require_matching_idempotent_intent(existing, requested_values)
    db.commit()
    db.refresh(intent)
    return intent


def decide_order_intent(
    db: Session,
    intent_id: uuid.UUID,
    *,
    scope: RequestScope | None = None,
    decision: str,
    decided_by: str,
    channel: str,
    expected_intent_hash: str,
    note: str | None = None,
    now: datetime | None = None,
) -> OrderApproval:
    if scope is None:
        legacy_intent = db.get(OrderIntent, intent_id)
        if legacy_intent is None:
            raise LookupError("order intent was not found")
        scope = RequestScope(
            workspace_id=legacy_intent.workspace_id,
            account_id=legacy_intent.account_id,
        )
    validate_scope(db, scope)
    intent = db.scalar(
        select(OrderIntent)
        .where(
            OrderIntent.workspace_id == scope.workspace_id,
            OrderIntent.account_id == scope.account_id,
            OrderIntent.id == intent_id,
        )
        .with_for_update()
    )
    if intent is None:
        raise LookupError("order intent not found")
    current_time = now or datetime.now(UTC)
    if intent.status != "proposed":
        raise InvalidIntentTransition(f"intent is already {intent.status}")
    if intent.expires_at is not None and intent.expires_at <= current_time:
        intent.status = "expired"
        db.commit()
        raise InvalidIntentTransition("intent has expired")
    calculated_hash = intent_hash(intent)
    if not hmac.compare_digest(calculated_hash, expected_intent_hash):
        raise InvalidIntentTransition("intent content changed or approval hash is invalid")
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    approval = OrderApproval(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        order_intent_id=intent.id,
        decision=decision,
        decided_by=decided_by,
        channel=channel,
        note=note,
        intent_hash=calculated_hash,
    )
    intent.status = decision
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval
