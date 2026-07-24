import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderApproval, OrderIntent, Trade, TradeManagementEvent
from app.schemas import ManagementEventCreate


class InvalidIntentTransition(ValueError):
    pass


def record_management_event(
    db: Session,
    trade_id: uuid.UUID,
    request: ManagementEventCreate,
) -> TradeManagementEvent:
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise LookupError("trade lifecycle not found")
    event = TradeManagementEvent(
        trade_id=trade.id,
        **request.model_dump(),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _intent_values(intent: OrderIntent) -> dict[str, str | None]:
    return {
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
        "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
    }


def intent_hash(intent: OrderIntent) -> str:
    serialized = json.dumps(_intent_values(intent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def propose_order_intent(
    db: Session,
    *,
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
    existing = db.scalar(
        select(OrderIntent).where(OrderIntent.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    if expires_at is not None and (
        expires_at.tzinfo is None or expires_at.utcoffset() is None
    ):
        raise ValueError("expires_at must be timezone-aware")
    intent = OrderIntent(
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
    db.add(intent)
    db.commit()
    db.refresh(intent)
    return intent


def decide_order_intent(
    db: Session,
    intent_id: uuid.UUID,
    *,
    decision: str,
    decided_by: str,
    channel: str,
    expected_intent_hash: str,
    note: str | None = None,
    now: datetime | None = None,
) -> OrderApproval:
    intent = db.scalar(
        select(OrderIntent).where(OrderIntent.id == intent_id).with_for_update()
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
