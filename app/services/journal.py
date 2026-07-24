import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TradePlan, TradeReflection
from app.schemas import ReflectionCreate, TradePlanCreate
from app.services.risk import calculate_position_size


class TradeNotFoundError(LookupError):
    pass


class ReflectionExistsError(ValueError):
    pass


def create_trade_plan(db: Session, request: TradePlanCreate) -> TradePlan:
    sizing = calculate_position_size(request)
    trade = TradePlan(
        **request.model_dump(exclude={"target"}),
        target=request.target,
        risk_amount=sizing.risk_amount,
        quantity=sizing.quantity,
        planned_r=sizing.planned_r,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def list_trade_plans(db: Session, limit: int | None = None) -> list[TradePlan]:
    statement = select(TradePlan).order_by(TradePlan.created_at.desc())
    if limit is not None:
        statement = statement.limit(limit)
    return list(db.scalars(statement))


def get_trade_plan(db: Session, trade_id: uuid.UUID) -> TradePlan:
    trade = db.get(TradePlan, trade_id)
    if not trade:
        raise TradeNotFoundError("trade not found")
    return trade


def create_reflection(
    db: Session,
    trade_id: uuid.UUID,
    request: ReflectionCreate,
) -> TradeReflection:
    trade = get_trade_plan(db, trade_id)
    if trade.reflection:
        raise ReflectionExistsError("reflection already exists")
    if trade.risk_amount == 0:
        raise ValueError("trade risk amount cannot be zero")

    realized_r = (request.realized_pnl / trade.risk_amount).quantize(Decimal("0.0001"))
    reflection = TradeReflection(
        trade_id=trade.id,
        realized_r=realized_r,
        **request.model_dump(),
    )
    trade.status = "reviewed"
    db.add(reflection)
    db.commit()
    db.refresh(reflection)
    return reflection
