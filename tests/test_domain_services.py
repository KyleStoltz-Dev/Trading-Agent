import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    InstrumentMapping,
    Observation,
    RuleEvaluation,
    Trade,
    TradingAccount,
)
from app.schemas import (
    BrokerPositionSizeRequest,
    InstrumentSpecificationCreate,
    ManagementEventCreate,
    ReflectionCreate,
    TradePlanCreate,
)
from app.services.analytics import build_edge_report
from app.services.catalog import (
    active_instrument_specification,
    configure_instrument_specification,
)
from app.services.execution_ledger import (
    InvalidIntentTransition,
    decide_order_intent,
    intent_hash,
    propose_order_intent,
    record_management_event,
)
from app.services.journal import create_reflection, create_trade_plan
from app.services.risk import calculate_broker_position_size


def _specification_request() -> InstrumentSpecificationCreate:
    return InstrumentSpecificationCreate(
        provider="oanda-v20",
        external_symbol="XAU_USD",
        venue="OANDA",
        canonical_symbol="XAUUSD",
        asset_class="metal",
        contract_size="1",
        tick_size="0.01",
        tick_value_per_quantity_unit="0.01",
        minimum_quantity="1",
        maximum_quantity="1000",
        quantity_step="1",
        margin_rate="0.05",
        estimated_spread="0.50",
        commission_per_quantity="0",
        pnl_currency="USD",
        source="broker-test-fixture",
    )


def _plan_request() -> TradePlanCreate:
    return TradePlanCreate(
        instrument="XAUUSD",
        venue="OANDA",
        direction="long",
        setup_name=f"reaccumulation-{uuid.uuid4()}",
        regime="range",
        session_name="New York",
        market_time=datetime(2026, 7, 23, 14, 30, tzinfo=UTC),
        context_timeframe="4h",
        trigger_timeframe="5m",
        entry="2400",
        stop="2390",
        target="2440",
        account_equity="10000",
        risk_percent="1",
        value_per_price_unit="1",
        sizing_provider="oanda-v20",
        sizing_symbol="XAU_USD",
        available_margin="10000",
        conversion_rate_to_account="1",
        estimated_slippage="0",
        thesis="Higher-timeframe range support held.",
        invalidation="Acceptance below the protected low.",
        observations=["Price traded below and reclaimed the reference low."],
        interpretations=["The move may be a spring."],
    )


def test_broker_contract_drives_plan_sizing_and_normalized_observations(db_session) -> None:
    configure_instrument_specification(db_session, _specification_request())
    specification = active_instrument_specification(
        db_session,
        provider="oanda-v20",
        external_symbol="XAU_USD",
    )
    sizing = calculate_broker_position_size(
        BrokerPositionSizeRequest(
            account_equity="10000",
            available_margin="10000",
            risk_percent="1",
            entry="2400",
            stop="2390",
            target="2440",
            maximum_risk_percent="1",
        ),
        specification,
    )

    plan = create_trade_plan(
        db_session,
        _plan_request(),
        policy_hash="a" * 64,
        source="test",
    )
    observations = list(
        db_session.scalars(
            select(Observation).where(Observation.trade_plan_id == plan.id)
        )
    )

    assert sizing.quantity == Decimal("9.0000000000")
    assert sizing.estimated_loss_at_stop == Decimal("90.00")
    assert sizing.estimated_costs == Decimal("4.50")
    assert plan.instrument_specification_id == specification.id
    assert plan.risk_amount == Decimal("94.5000")
    assert {item.kind for item in observations} == {"fact", "hypothesis"}


def test_review_normalizes_rules_and_edge_report_keeps_process_separate(db_session) -> None:
    configure_instrument_specification(db_session, _specification_request())
    plan = create_trade_plan(db_session, _plan_request(), policy_hash="b" * 64)
    reflection = create_reflection(
        db_session,
        plan.id,
        ReflectionCreate(
            exit_average="2440",
            realized_pnl="189",
            execution_grade="A",
            process_score="92",
            outcome_score="80",
            rule_adherence=[
                {"rule": "risk_accepted", "followed": True, "note": None},
                {"rule": "waited_for_trigger", "followed": False, "note": "early"},
            ],
            notes="Process and outcome are scored independently.",
        ),
    )
    evaluations = list(
        db_session.scalars(
            select(RuleEvaluation).where(
                RuleEvaluation.reflection_id == reflection.id
            )
        )
    )
    report = build_edge_report(db_session, minimum_sample=30)

    assert {item.result for item in evaluations} == {"met", "not_met"}
    segment = next(
        item for item in report.segments if item.setup_name == plan.setup_name
    )
    assert segment.expectancy_r == Decimal("2.0000")
    assert segment.process_score_average == Decimal("92.0000")
    assert segment.validated_sample is False


def test_management_and_approval_ledger_are_append_only(db_session) -> None:
    account = TradingAccount(
        broker="test",
        external_account_id=str(uuid.uuid4()),
        label="test",
        currency="USD",
        mode="practice",
    )
    db_session.add(account)
    db_session.flush()
    configure_instrument_specification(db_session, _specification_request())
    specification = active_instrument_specification(
        db_session,
        provider="oanda-v20",
        external_symbol="XAU_USD",
    )
    instrument_id = db_session.get(
        InstrumentMapping, specification.instrument_mapping_id
    ).instrument_id
    trade = Trade(
        account_id=account.id,
        instrument_id=instrument_id,
        direction="short",
        status="open",
        origin="manual",
    )
    db_session.add(trade)
    db_session.commit()
    event = record_management_event(
        db_session,
        trade.id,
        ManagementEventCreate(
            event_type="partial_taken",
            price="2360",
            quantity_delta="-3",
            position_quantity_after="-1",
            realized_r_at_event="4",
            reason="Paid 3/4 at the predefined objective.",
            occurred_at=datetime.now(UTC),
        ),
    )
    intent = propose_order_intent(
        db_session,
        trade_id=trade.id,
        action="modify_stop",
        side="buy",
        order_type="stop",
        quantity="1",
        stop_price="2400",
        rationale="Protect the remaining runner.",
        policy_hash="c" * 64,
        proposed_by="human",
        idempotency_key=str(uuid.uuid4()),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    approval = decide_order_intent(
        db_session,
        intent.id,
        decision="approved",
        decided_by="trader",
        channel="cli",
        expected_intent_hash=intent_hash(intent),
    )

    assert event.realized_r_at_event == Decimal("4.0000")
    assert approval.decision == "approved"
    with pytest.raises(InvalidIntentTransition, match="already"):
        decide_order_intent(
            db_session,
            intent.id,
            decision="approved",
            decided_by="trader",
            channel="cli",
            expected_intent_hash=intent_hash(intent),
        )
