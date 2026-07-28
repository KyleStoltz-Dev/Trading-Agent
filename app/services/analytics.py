import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TradePlan, TradeReflection
from app.schemas import EdgeReport, EdgeSegment
from app.services.workspaces import (
    RequestScope,
    validate_strategy_scope,
)


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal("0")) / len(values)).quantize(Decimal("0.0001"))


def _news_bucket(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    direction = "before" if minutes >= 0 else "after"
    distance = abs(minutes)
    if distance <= 15:
        return f"{direction}_0_15m"
    if distance <= 60:
        return f"{direction}_16_60m"
    if distance <= 240:
        return f"{direction}_1_4h"
    return f"{direction}_over_4h"


def build_edge_report(
    db: Session,
    *,
    scope: RequestScope,
    minimum_sample: int = 30,
    playbook_version_id: uuid.UUID | None = None,
) -> EdgeReport:
    validate_strategy_scope(db, scope, playbook_version_id)
    statement = (
        select(TradePlan, TradeReflection)
        .join(
            TradeReflection,
            (TradeReflection.workspace_id == TradePlan.workspace_id)
            & (TradeReflection.account_id == TradePlan.account_id)
            & (TradeReflection.trade_id == TradePlan.id),
        )
        .where(
            TradePlan.workspace_id == scope.workspace_id,
            TradePlan.account_id == scope.account_id,
            TradeReflection.workspace_id == scope.workspace_id,
            TradeReflection.account_id == scope.account_id,
        )
        .order_by(TradePlan.created_at)
    )
    if playbook_version_id is not None:
        statement = statement.where(
            TradePlan.playbook_version_id == playbook_version_id
        )
    rows = db.execute(statement).all()
    grouped: dict[tuple, list[TradeReflection]] = defaultdict(list)
    for plan, reflection in rows:
        key = (
            plan.setup_name,
            plan.instrument,
            plan.regime,
            plan.context_timeframe,
            plan.trigger_timeframe,
            plan.session_name,
            plan.playbook_version_id,
            _news_bucket(plan.minutes_to_high_impact_event),
        )
        grouped[key].append(reflection)

    segments = []
    for key, reflections in grouped.items():
        realized = [item.realized_r for item in reflections]
        wins = [value for value in realized if value > 0]
        losses = [value for value in realized if value < 0]
        breakeven = len(realized) - len(wins) - len(losses)
        process_scores = [
            item.process_score for item in reflections if item.process_score is not None
        ]
        sample_size = len(realized)
        segments.append(
            EdgeSegment(
                setup_name=key[0],
                instrument=key[1],
                regime=key[2],
                context_timeframe=key[3],
                trigger_timeframe=key[4],
                session_name=key[5],
                playbook_version_id=key[6],
                news_proximity_bucket=key[7],
                sample_size=sample_size,
                wins=len(wins),
                losses=len(losses),
                breakeven=breakeven,
                win_rate=(
                    Decimal(len(wins)) / Decimal(sample_size) * Decimal("100")
                ).quantize(Decimal("0.01")),
                expectancy_r=_average(realized) or Decimal("0"),
                average_win_r=_average(wins),
                average_loss_r=_average(losses),
                process_score_average=_average(process_scores),
                validated_sample=sample_size >= minimum_sample,
            )
        )
    segments.sort(key=lambda item: (-item.sample_size, item.setup_name, item.instrument))
    return EdgeReport(
        minimum_sample=minimum_sample,
        total_reviewed=len(rows),
        segments=segments,
    )
