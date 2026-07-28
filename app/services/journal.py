import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import LEGACY_UNASSIGNED_ACCOUNT_ID, LEGACY_WORKSPACE_ID
from app.models import (
    EconomicEvent,
    Observation,
    Playbook,
    RuleEvaluation,
    TradePlan,
    TradeReflection,
)
from app.schemas import BrokerPositionSizeRequest, ReflectionCreate, TradePlanCreate
from app.services.catalog import (
    active_instrument_specification,
    get_or_create_instrument,
    verify_playbook_version_integrity,
)
from app.services.event_relevance import instrument_event_currencies
from app.services.risk import calculate_broker_position_size, calculate_position_size
from app.services.strategy_risk import effective_strategy_risk_policy
from app.services.workspaces import (
    RequestScope,
    validate_scope,
    validate_strategy_scope,
)


class TradeNotFoundError(LookupError):
    pass


class ReflectionExistsError(ValueError):
    pass


def _legacy_scope(scope: RequestScope | None) -> RequestScope:
    return scope or RequestScope(
        workspace_id=uuid.UUID(LEGACY_WORKSPACE_ID),
        account_id=uuid.UUID(LEGACY_UNASSIGNED_ACCOUNT_ID),
    )


def _reference_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _session_reference(value: str | None) -> str:
    words = re.findall(r"[a-z0-9]+", (value or "all").lower())
    if not words:
        return "all"
    if len(words) > 1:
        return "".join(word[0] for word in words)
    return words[0][:12]


def next_trade_reference(
    db: Session,
    request: TradePlanCreate,
    *,
    scope: RequestScope | None = None,
) -> str:
    explicit_scope = scope is not None
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    occurred_at = request.market_time or datetime.now(UTC)
    prefix = "-".join(
        (
            _reference_part(request.instrument),
            occurred_at.strftime("%Y%m%d"),
            _session_reference(request.session_name),
            request.direction,
        )
    )
    # The transaction-scoped PostgreSQL advisory lock serializes allocation within
    # one account/reference prefix without blocking unrelated accounts or sessions.
    lock_scope = (
        f"{scope.workspace_id}:{scope.account_id}:{prefix}"
        if explicit_scope
        else prefix
    )
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:prefix, 0))"),
        {"prefix": lock_scope},
    )
    references = db.scalars(
        select(TradePlan.reference).where(
            TradePlan.workspace_id == scope.workspace_id,
            TradePlan.account_id == scope.account_id,
            TradePlan.reference.like(f"{prefix}-%"),
        )
    )
    suffixes = [
        int(reference.rsplit("-", 1)[-1])
        for reference in references
        if reference.rsplit("-", 1)[-1].isdigit()
    ]
    return f"{prefix}-{max(suffixes, default=0) + 1}"


def create_trade_plan(
    db: Session,
    request: TradePlanCreate,
    *,
    scope: RequestScope | None = None,
    policy_hash: str | None = None,
    source: str = "manual",
    maximum_risk_percent: Decimal = Decimal("1"),
    playbook_version_id: uuid.UUID | None = None,
    commit: bool = True,
) -> TradePlan:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    playbook_version = None
    minimum_planned_r = None
    if playbook_version_id is not None:
        playbook_version = validate_strategy_scope(
            db,
            scope,
            playbook_version_id,
        )
        if playbook_version is None:
            raise LookupError("strategy version was not found")
        verify_playbook_version_integrity(playbook_version)
        playbook = db.scalar(
            select(Playbook).where(
                Playbook.workspace_id == scope.workspace_id,
                Playbook.id == playbook_version.playbook_id,
            )
        )
        if playbook is None or playbook.name != request.setup_name:
            raise ValueError(
                "trade setup_name must match the exact strategy active in this session"
            )
        maximum_risk_percent, minimum_planned_r = effective_strategy_risk_policy(
            playbook_version.definition,
            maximum_risk_percent=maximum_risk_percent,
        )
    if request.risk_percent > maximum_risk_percent:
        raise ValueError("requested risk exceeds the effective strategy maximum")
    specification = None
    estimated_costs = None
    estimated_margin = None
    if request.sizing_provider and request.sizing_symbol:
        try:
            specification = active_instrument_specification(
                db,
                provider=request.sizing_provider,
                external_symbol=request.sizing_symbol,
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
            )
        except LookupError:
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
    if minimum_planned_r is not None and (
        planned_r is None or planned_r < minimum_planned_r
    ):
        raise ValueError(
            f"planned R must be at least {minimum_planned_r} for the active strategy"
        )
    instrument = get_or_create_instrument(db, request.instrument)
    minutes_to_event = None
    if request.market_time is not None:
        relevant_currencies = instrument_event_currencies(request.instrument)
        event = db.scalar(
            select(EconomicEvent)
            .where(
                EconomicEvent.importance == 3,
                func.upper(EconomicEvent.currency).in_(relevant_currencies),
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
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        reference=next_trade_reference(db, request, scope=scope),
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
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                trade_plan_id=trade.id,
                kind="fact",
                text=value,
                actor_type="human",
            )
            for value in request.observations
        ]
        + [
            Observation(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                trade_plan_id=trade.id,
                kind="hypothesis",
                text=value,
                actor_type="human",
            )
            for value in request.interpretations
        ]
    )
    if commit:
        db.commit()
        db.refresh(trade)
    return trade


def list_trade_plans(
    db: Session,
    limit: int | None = None,
    *,
    scope: RequestScope | None = None,
    playbook_version_id: uuid.UUID | None = None,
) -> list[TradePlan]:
    scope = _legacy_scope(scope)
    validate_strategy_scope(db, scope, playbook_version_id)
    statement = (
        select(TradePlan)
        .where(
            TradePlan.workspace_id == scope.workspace_id,
            TradePlan.account_id == scope.account_id,
        )
        .order_by(TradePlan.created_at.desc())
    )
    if playbook_version_id is not None:
        statement = statement.where(
            TradePlan.playbook_version_id == playbook_version_id
        )
    if limit is not None:
        statement = statement.limit(limit)
    return list(db.scalars(statement))


def get_trade_plan(
    db: Session,
    trade_reference: str | uuid.UUID,
    *,
    scope: RequestScope | None = None,
    playbook_version_id: uuid.UUID | None = None,
) -> TradePlan:
    scope = _legacy_scope(scope)
    validate_strategy_scope(db, scope, playbook_version_id)
    try:
        trade_id = (
            trade_reference
            if isinstance(trade_reference, uuid.UUID)
            else uuid.UUID(trade_reference)
        )
    except ValueError:
        trade = db.scalar(
            select(TradePlan).where(
                TradePlan.workspace_id == scope.workspace_id,
                TradePlan.account_id == scope.account_id,
                TradePlan.reference == str(trade_reference).lower(),
            )
        )
    else:
        trade = db.scalar(
            select(TradePlan).where(
                TradePlan.workspace_id == scope.workspace_id,
                TradePlan.account_id == scope.account_id,
                TradePlan.id == trade_id,
            )
        )
    if not trade or (
        playbook_version_id is not None
        and trade.playbook_version_id != playbook_version_id
    ):
        raise TradeNotFoundError(f"trade plan not found: {trade_reference}")
    return trade


def create_reflection(
    db: Session,
    trade_id: str | uuid.UUID,
    request: ReflectionCreate,
    *,
    scope: RequestScope | None = None,
    playbook_version_id: uuid.UUID | None = None,
) -> TradeReflection:
    scope = _legacy_scope(scope)
    trade = get_trade_plan(
        db,
        trade_id,
        scope=scope,
        playbook_version_id=playbook_version_id,
    )
    if trade.reflection:
        raise ReflectionExistsError("reflection already exists")
    if trade.risk_amount == 0:
        raise ValueError("trade risk amount cannot be zero")

    realized_r = (request.realized_pnl / trade.risk_amount).quantize(Decimal("0.0001"))
    reflection = TradeReflection(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
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
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
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
