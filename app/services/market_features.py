import math
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.market_data.contracts import Candle
from app.models import StrategyTestSample
from app.services.strategy_workspace import resolve_strategy_experiment
from app.services.workspaces import RequestScope, validate_scope


def _decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def measure_candle_features(
    candles: list[Candle],
    *,
    equal_level_atr_fraction: Decimal = Decimal("0.10"),
    displacement_body_multiplier: Decimal = Decimal("2"),
) -> dict[str, Any]:
    ordered = sorted((item for item in candles if item.complete), key=lambda item: item.started_at)
    if len(ordered) < 3:
        raise ValueError("at least three complete candles are required")
    true_ranges: list[Decimal] = []
    bodies: list[Decimal] = []
    for index, candle in enumerate(ordered):
        previous_close = ordered[index - 1].close if index else candle.open
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
        bodies.append(abs(candle.close - candle.open))
    atr_period = min(14, len(true_ranges))
    atr = sum(true_ranges[-atr_period:], Decimal("0")) / Decimal(atr_period)
    tolerance = atr * equal_level_atr_fraction
    median_body = Decimal(str(median(bodies)))

    imbalances = []
    for index in range(2, len(ordered)):
        first = ordered[index - 2]
        third = ordered[index]
        if third.low > first.high:
            imbalances.append(
                {
                    "direction": "bullish",
                    "started_at": third.started_at.isoformat(),
                    "lower": _decimal(first.high),
                    "upper": _decimal(third.low),
                    "size": _decimal(third.low - first.high),
                }
            )
        elif third.high < first.low:
            imbalances.append(
                {
                    "direction": "bearish",
                    "started_at": third.started_at.isoformat(),
                    "lower": _decimal(third.high),
                    "upper": _decimal(first.low),
                    "size": _decimal(first.low - third.high),
                }
            )

    equal_levels = []
    for index in range(1, len(ordered)):
        prior = ordered[index - 1]
        current = ordered[index]
        if abs(current.high - prior.high) <= tolerance:
            equal_levels.append(
                {
                    "side": "high",
                    "first": prior.started_at.isoformat(),
                    "second": current.started_at.isoformat(),
                    "level": _decimal((prior.high + current.high) / Decimal("2")),
                    "distance": _decimal(abs(current.high - prior.high)),
                }
            )
        if abs(current.low - prior.low) <= tolerance:
            equal_levels.append(
                {
                    "side": "low",
                    "first": prior.started_at.isoformat(),
                    "second": current.started_at.isoformat(),
                    "level": _decimal((prior.low + current.low) / Decimal("2")),
                    "distance": _decimal(abs(current.low - prior.low)),
                }
            )

    sweeps = []
    lookback = min(20, len(ordered) - 1)
    for index in range(1, len(ordered)):
        prior = ordered[max(0, index - lookback) : index]
        prior_high = max(item.high for item in prior)
        prior_low = min(item.low for item in prior)
        candle = ordered[index]
        if candle.high > prior_high and candle.close < prior_high:
            sweeps.append(
                {
                    "side": "high",
                    "started_at": candle.started_at.isoformat(),
                    "reference": _decimal(prior_high),
                    "extreme": _decimal(candle.high),
                    "rejection": _decimal(prior_high - candle.close),
                }
            )
        if candle.low < prior_low and candle.close > prior_low:
            sweeps.append(
                {
                    "side": "low",
                    "started_at": candle.started_at.isoformat(),
                    "reference": _decimal(prior_low),
                    "extreme": _decimal(candle.low),
                    "rejection": _decimal(candle.close - prior_low),
                }
            )

    displacement = [
        {
            "started_at": candle.started_at.isoformat(),
            "direction": "up" if candle.close > candle.open else "down",
            "body": _decimal(body),
            "body_to_median": _decimal(body / median_body) if median_body else None,
        }
        for candle, body in zip(ordered, bodies, strict=True)
        if median_body and body >= median_body * displacement_body_multiplier
    ]
    first_close = ordered[0].close
    last_close = ordered[-1].close
    return {
        "definition": {
            "imbalance": "three-candle gap: candle 3 low above candle 1 high, or inverse",
            "equal_level_tolerance": (
                f"{equal_level_atr_fraction} of {atr_period}-period average true range"
            ),
            "sweep": "trades beyond rolling prior extreme and closes back through it",
            "displacement": (
                f"body at least {displacement_body_multiplier} times median candle body"
            ),
        },
        "instrument": ordered[-1].instrument,
        "timeframe": ordered[-1].timeframe,
        "from": ordered[0].started_at.isoformat(),
        "to": ordered[-1].started_at.isoformat(),
        "candle_count": len(ordered),
        "atr": _decimal(atr),
        "range_high": _decimal(max(item.high for item in ordered)),
        "range_low": _decimal(min(item.low for item in ordered)),
        "close_change": _decimal(last_close - first_close),
        "close_change_percent": (
            _decimal((last_close - first_close) / first_close * Decimal("100"))
            if first_close
            else None
        ),
        "imbalances": imbalances,
        "equal_levels": equal_levels,
        "sweeps": sweeps,
        "displacement_candles": displacement,
        "source": ordered[-1].source,
        "retrieved_at": ordered[-1].retrieved_at.isoformat(),
    }


def experiment_feature_correlations(
    db: Session,
    experiment_id,
    *,
    scope: RequestScope,
    minimum_samples: int = 10,
) -> dict[str, Any]:
    validate_scope(db, scope)
    samples = list(
        db.scalars(
            select(StrategyTestSample).where(
                StrategyTestSample.workspace_id == scope.workspace_id,
                StrategyTestSample.account_id == scope.account_id,
                StrategyTestSample.experiment_id == experiment_id,
                StrategyTestSample.classification == "eligible",
                StrategyTestSample.outcome_r.is_not(None),
            )
        )
    )
    if len(samples) < minimum_samples:
        return {
            "sample_size": len(samples),
            "minimum_samples": minimum_samples,
            "validated": False,
            "correlations": {},
        }
    feature_values: dict[str, list[tuple[float, float]]] = {}
    for sample in samples:
        outcome = float(sample.outcome_r)
        for key, value in sample.feature_snapshot.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                feature_values.setdefault(key, []).append((float(value), outcome))
    correlations = {}
    for key, pairs in feature_values.items():
        if len(pairs) < minimum_samples:
            continue
        xs = [pair[0] for pair in pairs]
        ys = [pair[1] for pair in pairs]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        denominator = math.sqrt(
            sum((x - mean_x) ** 2 for x in xs)
            * sum((y - mean_y) ** 2 for y in ys)
        )
        correlations[key] = round(numerator / denominator, 4) if denominator else None
    return {
        "sample_size": len(samples),
        "minimum_samples": minimum_samples,
        "validated": True,
        "correlations": correlations,
        "warning": "Correlation is descriptive, not causal, and must be checked out of sample.",
    }


def strategy_experiment_report(
    db: Session,
    experiment_id,
    *,
    scope: RequestScope,
    active_playbook_version_id=None,
) -> dict[str, Any]:
    experiment = resolve_strategy_experiment(
        db,
        experiment_id,
        scope=scope,
        playbook_version_id=active_playbook_version_id,
    )
    experiment_id = experiment.id
    samples = list(
        db.scalars(
            select(StrategyTestSample)
            .where(
                StrategyTestSample.workspace_id == scope.workspace_id,
                StrategyTestSample.account_id == scope.account_id,
                StrategyTestSample.experiment_id == experiment_id,
            )
            .order_by(StrategyTestSample.occurred_at)
        )
    )
    classifications = {
        key: sum(sample.classification == key for sample in samples)
        for key in ("eligible", "excluded", "unclear")
    }
    outcomes = [
        Decimal(sample.outcome_r)
        for sample in samples
        if sample.classification == "eligible" and sample.outcome_r is not None
    ]
    wins = sum(value > 0 for value in outcomes)
    losses = sum(value < 0 for value in outcomes)
    breakeven = sum(value == 0 for value in outcomes)
    resolved = wins + losses
    return {
        "experiment_id": str(experiment.id),
        "playbook_version_id": str(experiment.playbook_version_id),
        "name": experiment.name,
        "mode": experiment.mode,
        "status": experiment.status,
        "hypothesis": experiment.hypothesis,
        "instrument": experiment.instrument,
        "timeframe": experiment.timeframe,
        "rules_hash": experiment.rules_hash,
        "sample_count": len(samples),
        "classifications": classifications,
        "outcomes_recorded": len(outcomes),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": (
            format(Decimal(wins) / Decimal(resolved) * Decimal("100"), ".2f")
            if resolved
            else None
        ),
        "expectancy_r": (
            format(sum(outcomes, Decimal("0")) / Decimal(len(outcomes)), ".4f")
            if outcomes
            else None
        ),
        "feature_correlations": experiment_feature_correlations(
            db,
            experiment_id,
            scope=scope,
        ),
        "warning": (
            "This report describes the frozen sample. It does not establish causation "
            "or guarantee forward performance."
        ),
    }
