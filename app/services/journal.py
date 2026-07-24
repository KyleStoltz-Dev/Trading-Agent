import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    EconomicEvent,
    Observation,
    Playbook,
    PlaybookVersion,
    RuleEvaluation,
    TradePlan,
    TradeReflection,
)
from app.schemas import BrokerPositionSizeRequest, ReflectionCreate, TradePlanCreate
from app.services.catalog import (
    active_instrument_specification,
    get_or_create_instrument,
)
from app.services.risk import calculate_broker_position_size, calculate_position_size


class TradeNotFoundError(LookupError):
    pass


class ReflectionExistsError(ValueError):
    pass


def create_trade_plan(
    db: Session,
    request: TradePlanCreate,
    *,
    policy_hash: str | None = None,
    source: str = "manual",
    maximum_risk_percent: Decimal = Decimal("1"),
) -> TradePlan:
    if request.risk_percent > maximum_risk_percent:
        raise ValueError("requested risk exceeds the configured maximum")
    specification = None
    estimated_costs = None
    estimated_margin = None
    if request.sizing_provider and request.sizing_symbol:
        specification = active_instrument_specification(
            db,
            provider=request.sizing_provider,
            external_symbol=request.sizing_symbol,
        )
        broker_sizing = calculate_broker_position_size(
            BrokerPositionSizeRequest(
                account_equity=request.account_equity,
                available_margin=request.available_margin,
                risk_percent=request.risk_percent,
                entry=request.entry,
                stop=request.stop,
                target=request.target,
                conversion_rate_to_account=request.conversion_rate_to_account,
                estimated_slippage=request.estimated_slippage,
                maximum_risk_percent=maximum_risk_percent,
            ),
            specification,
        )
        risk_amount = (
            broker_sizing.estimated_loss_at_stop + broker_sizing.estimated_costs
        )
        quantity = broker_sizing.quantity
        planned_r = broker_sizing.planned_r
        estimated_costs = broker_sizing.estimated_costs
        estimated_margin = broker_sizing.estimated_margin
    else:
        sizing = calculate_position_size(request)
        risk_amount = sizing.risk_amount
        quantity = sizing.quantity
        planned_r = sizing.planned_r
    instrument = get_or_create_instrument(db, request.instrument)
    playbook_version = db.scalar(
        select(PlaybookVersion)
        .join(Playbook)
        .where(Playbook.name == request.setup_name)
        .order_by(PlaybookVersion.version.desc())
    )
    minutes_to_event = None
    if request.market_time is not None:
        event = db.scalar(
            select(EconomicEvent)
            .where(
                EconomicEvent.importance == 3,
                EconomicEvent.scheduled_at
                >= request.market_time - timedelta(hours=24),
                EconomicEvent.scheduled_at
                <= request.market_time + timedelta(hours=24),
            )
            .order_by(
                func.abs(
                    func.extract(
                        "epoch",
                        EconomicEvent.scheduled_at - request.market_time,
                    )
                )
            )
        )
        if event is not None:
            minutes_to_event = int(
                (event.scheduled_at - request.market_time).total_seconds() / 60
            )
    trade = TradePlan(
        **request.model_dump(
            exclude={
                "target",
                "market_time",
                "sizing_provider",
                "sizing_symbol",
                "available_margin",
                "conversion_rate_to_account",
                "estimated_slippage",
            }
        ),
        target=request.target,
        instrument_id=instrument.id,
        playbook_version_id=playbook_version.id if playbook_version else None,
        instrument_specification_id=specification.id if specification else None,
        risk_amount=risk_amount,
        quantity=quantity,
        planned_r=planned_r,
        estimated_costs=estimated_costs,
        estimated_margin=estimated_margin,
        policy_hash=policy_hash,
        source=source,
        source_time=request.market_time,
        minutes_to_high_impact_event=minutes_to_event,
    )
    db.add(trade)
    db.flush()
    db.add_all(
        [
            Observation(
                trade_plan_id=trade.id,
                kind="fact",
                text=value,
                actor_type="human",
            )
            for value in request.observations
        ]
        + [
            Observation(
                trade_plan_id=trade.id,
                kind="hypothesis",
                text=value,
                actor_type="human",
            )
            for value in request.interpretations
        ]
    )
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
        outcome_grade=(
            "win"
            if request.realized_pnl > 0
            else "loss"
            if request.realized_pnl < 0
            else "breakeven"
        ),
        **request.model_dump(),
    )
    trade.status = "reviewed"
    db.add(reflection)
    db.flush()
    for evaluation in request.rule_adherence:
        followed = evaluation.get("followed")
        db.add(
            RuleEvaluation(
                reflection_id=reflection.id,
                playbook_version_id=trade.playbook_version_id,
                rule_key=str(evaluation.get("rule", "unspecified")),
                result=(
                    "met"
                    if followed is True
                    else "not_met"
                    if followed is False
                    else "unclear"
                ),
                note=evaluation.get("note"),
            )
        )
    db.commit()
    db.refresh(reflection)
    return reflection
