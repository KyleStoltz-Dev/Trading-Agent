import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from app.market_data.contracts import Candle
from app.services.market_features import (
    experiment_feature_correlations,
    measure_candle_features,
    strategy_experiment_report,
)
from app.services.workspaces import RequestScope

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
TEST_SCOPE = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())


def candle(
    index: int,
    open_price: str,
    high: str,
    low: str,
    close: str,
) -> Candle:
    return Candle(
        instrument="XAU_USD",
        timeframe="M5",
        started_at=NOW + timedelta(minutes=5 * index),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        complete=True,
        retrieved_at=NOW + timedelta(hours=1),
        source="test-feed",
        venue="TEST",
    )


def test_measured_features_use_explicit_reproducible_definitions() -> None:
    result = measure_candle_features(
        [
            candle(0, "100", "101", "99", "100"),
            candle(1, "100", "102", "100", "101"),
            candle(2, "103", "104", "103", "104"),
            candle(3, "104", "105", "101", "102"),
        ]
    )

    assert result["instrument"] == "XAU_USD"
    assert result["candle_count"] == 4
    assert result["imbalances"][0]["direction"] == "bullish"
    assert "three-candle gap" in result["definition"]["imbalance"]
    assert result["source"] == "test-feed"


class SampleSession:
    def __init__(self, samples) -> None:
        self.samples = samples

    def scalars(self, statement):
        del statement
        return self.samples


def test_feature_correlations_require_a_sample_and_do_not_claim_causation(
    monkeypatch,
) -> None:
    samples = [
        SimpleNamespace(
            outcome_r=Decimal(str(index)),
            feature_snapshot={"displacement_ratio": float(index), "has_sweep": bool(index % 2)},
        )
        for index in range(1, 11)
    ]

    monkeypatch.setattr(
        "app.services.market_features.validate_scope",
        lambda *args, **kwargs: None,
    )
    result = experiment_feature_correlations(
        SampleSession(samples),
        "experiment",
        scope=TEST_SCOPE,
        minimum_samples=10,
    )

    assert result["validated"] is True
    assert result["correlations"]["displacement_ratio"] == 1.0
    assert "not causal" in result["warning"]
    assert "has_sweep" not in result["correlations"]


class ExperimentSession(SampleSession):
    def __init__(self, experiment, samples) -> None:
        super().__init__(samples)
        self.experiment = experiment

    def get(self, model, identifier):
        del model, identifier
        return self.experiment


def test_experiment_report_enforces_strategy_isolation(monkeypatch) -> None:
    active_version = uuid.uuid4()
    experiment_id = uuid.uuid4()
    experiment = SimpleNamespace(
        id=experiment_id,
        playbook_version_id=uuid.uuid4(),
        name="London replay",
        mode="backtest",
        status="completed",
        hypothesis="A spring plus displacement improves expectancy.",
        instrument="XAU_USD",
        timeframe="M5",
        rules_hash="a" * 64,
    )
    db = ExperimentSession(experiment, [])
    resolver = Mock(side_effect=PermissionError("different strategy version"))
    monkeypatch.setattr(
        "app.services.market_features.resolve_strategy_experiment",
        resolver,
    )

    try:
        strategy_experiment_report(
            db,
            experiment_id,
            scope=TEST_SCOPE,
            active_playbook_version_id=active_version,
        )
    except PermissionError as exc:
        assert "different strategy version" in str(exc)
    else:
        raise AssertionError("cross-strategy experiment access must fail closed")
    assert resolver.call_args.kwargs == {
        "scope": TEST_SCOPE,
        "playbook_version_id": active_version,
    }
