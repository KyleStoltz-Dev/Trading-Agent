import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import (
    Instrument,
    Playbook,
    PlaybookVersion,
    Trade,
    TradePlan,
    TradeReflection,
    TradingAccount,
    Workspace,
)
from app.schemas import ChartAnalysis, PlaybookCheck, TradingViewAlertCreate
from app.services.analytics import build_edge_report
from app.services.evidence import record_chart_analysis
from app.services.execution_ledger import (
    InvalidIntentTransition,
    decide_order_intent,
    intent_hash,
    propose_order_intent,
)
from app.services.integration_verification import integration_verifications
from app.services.tradingview import (
    ingest_tradingview_alert,
    recent_tradingview_alerts,
)
from app.services.workspaces import RequestScope


def _scope_pair(
    db_session,
) -> tuple[RequestScope, RequestScope, Instrument]:
    workspace = Workspace(
        slug=f"remaining-services-{uuid.uuid4().hex}",
        name="Remaining service scope tests",
    )
    db_session.add(workspace)
    db_session.flush()
    accounts = [
        TradingAccount(
            workspace_id=workspace.id,
            broker="test",
            external_account_id=f"account-{suffix}-{uuid.uuid4().hex}",
            label=f"Account {suffix}",
            currency="USD",
            mode="practice",
        )
        for suffix in ("A", "B")
    ]
    instrument = Instrument(
        canonical_symbol=f"XAUUSD-{uuid.uuid4().hex}",
        display_name="Gold",
        asset_class="commodity",
        base_currency="XAU",
        quote_currency="USD",
        price_precision=2,
        quantity_precision=2,
    )
    db_session.add_all([*accounts, instrument])
    db_session.flush()
    return (
        RequestScope(workspace.id, accounts[0].id),
        RequestScope(workspace.id, accounts[1].id),
        instrument,
    )


def _alert_payload() -> TradingViewAlertCreate:
    return TradingViewAlertCreate.model_validate(
        {
            "event_id": f"event-{uuid.uuid4().hex}",
            "alert_name": "Spring candidate",
            "symbol": "XAUUSD",
            "timeframe": "5",
            "event_type": "spring_candidate",
            "market_time": datetime.now(UTC),
            "open": "2398",
            "high": "2402",
            "low": "2395",
            "close": "2400",
        }
    )


def test_tradingview_replays_and_history_are_account_scoped(db_session) -> None:
    scope_a, scope_b, _ = _scope_pair(db_session)
    payload = _alert_payload()

    alert_a, created_a = ingest_tradingview_alert(
        db_session,
        payload,
        scope=scope_a,
        verified_source_ip="52.89.214.238",
    )
    assert recent_tradingview_alerts(db_session, scope=scope_b) == []
    reports_a = integration_verifications(Settings(), db_session, scope=scope_a)
    reports_b = integration_verifications(Settings(), db_session, scope=scope_b)
    assert next(item for item in reports_a if item.key == "tradingview").evidence == (
        "observed"
    )
    assert next(item for item in reports_b if item.key == "tradingview").evidence == (
        "not observed"
    )

    alert_b, created_b = ingest_tradingview_alert(
        db_session,
        payload,
        scope=scope_b,
        verified_source_ip="52.89.214.238",
    )

    assert created_a is True
    assert created_b is True
    assert alert_a.id != alert_b.id
    assert recent_tradingview_alerts(db_session, scope=scope_a) == [alert_a]
    assert recent_tradingview_alerts(db_session, scope=scope_b) == [alert_b]


def test_order_intents_cannot_cross_accounts_or_rebind_idempotency(
    db_session,
) -> None:
    scope_a, scope_b, instrument = _scope_pair(db_session)
    trade = Trade(
        workspace_id=scope_a.workspace_id,
        account_id=scope_a.account_id,
        instrument_id=instrument.id,
        direction="long",
        status="open",
        origin="manual",
    )
    db_session.add(trade)
    db_session.commit()
    key = f"intent-{uuid.uuid4().hex}"
    values = {
        "scope": scope_a,
        "trade_id": trade.id,
        "action": "modify_stop",
        "side": "sell",
        "order_type": "stop",
        "quantity": Decimal("1"),
        "stop_price": Decimal("2395"),
        "rationale": "Human-reviewed stop proposal.",
        "policy_hash": "a" * 64,
        "proposed_by": "human",
        "idempotency_key": key,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    intent = propose_order_intent(db_session, **values)

    with pytest.raises(LookupError, match="requested account"):
        propose_order_intent(db_session, **{**values, "scope": scope_b})
    with pytest.raises(InvalidIntentTransition, match="different order intent"):
        propose_order_intent(
            db_session,
            **{**values, "action": "close"},
        )
    with pytest.raises(LookupError, match="not found"):
        decide_order_intent(
            db_session,
            intent.id,
            scope=scope_b,
            decision="approved",
            decided_by="trader",
            channel="cli",
            expected_intent_hash=intent_hash(intent),
        )


def test_chart_evidence_files_and_rows_are_account_scoped(
    db_session,
    tmp_path,
) -> None:
    scope_a, scope_b, _ = _scope_pair(db_session)
    analysis = ChartAnalysis(
        visible_facts=["Price reclaimed the marked low."],
        unreadable_or_missing=[],
        context_hypotheses=["The range may be reaccumulation."],
        trigger_hypotheses=[],
        playbook_checks=[
            PlaybookCheck(check="spring", status="unclear", evidence=[])
        ],
        risk_questions=[],
        management_questions=[],
        disclaimer="Decision support only.",
    )
    arguments = {
        "image_bytes": b"\x89PNG\r\n\x1a\nscoped-test",
        "content_type": "image/png",
        "evidence_directory": tmp_path / "evidence",
        "analysis": analysis,
        "provider": SimpleNamespace(name="test-provider", model="test-model"),
        "policy_hash": "b" * 64,
        "prompt": "Separate observations and hypotheses.",
        "source": "test",
        "market_time": datetime.now(UTC),
        "instrument": "XAUUSD",
        "venue": "OANDA",
        "timeframe": "M5",
    }

    evidence_a, _ = record_chart_analysis(db_session, scope=scope_a, **arguments)
    evidence_b, _ = record_chart_analysis(db_session, scope=scope_b, **arguments)

    assert evidence_a.id != evidence_b.id
    assert evidence_a.workspace_id == evidence_b.workspace_id
    assert evidence_a.account_id != evidence_b.account_id
    assert evidence_a.storage_uri != evidence_b.storage_uri


def test_edge_report_only_uses_the_requested_account(db_session) -> None:
    scope_a, scope_b, _ = _scope_pair(db_session)
    playbook = Playbook(
        workspace_id=scope_a.workspace_id,
        name=f"Wyckoff-{uuid.uuid4().hex}",
        description="Scope test.",
    )
    db_session.add(playbook)
    db_session.flush()
    version = PlaybookVersion(
        workspace_id=scope_a.workspace_id,
        playbook_id=playbook.id,
        version=1,
        definition={"setups": []},
        content_hash="c" * 64,
    )
    db_session.add(version)
    db_session.flush()

    for scope, setup, realized_r in (
        (scope_a, "account-a-setup", Decimal("2")),
        (scope_b, "account-b-setup", Decimal("-1")),
    ):
        plan = TradePlan(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            reference=f"plan-{uuid.uuid4().hex}",
            playbook_version_id=version.id,
            instrument="XAUUSD",
            direction="long",
            setup_name=setup,
            context_timeframe="H1",
            trigger_timeframe="M5",
            entry=Decimal("2400"),
            stop=Decimal("2395"),
            target=Decimal("2410"),
            account_equity=Decimal("10000"),
            risk_percent=Decimal("1"),
            value_per_price_unit=Decimal("1"),
            risk_amount=Decimal("100"),
            quantity=Decimal("20"),
            planned_r=Decimal("2"),
            thesis="Scoped hypothesis.",
            invalidation="Scoped invalidation.",
        )
        db_session.add(plan)
        db_session.flush()
        db_session.add(
            TradeReflection(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                trade_id=plan.id,
                exit_average=Decimal("2410"),
                realized_pnl=realized_r * Decimal("100"),
                realized_r=realized_r,
                execution_grade="A",
                notes="Scoped review.",
            )
        )
    db_session.commit()

    report = build_edge_report(
        db_session,
        scope=scope_a,
        minimum_sample=1,
        playbook_version_id=version.id,
    )
    assert report.total_reviewed == 1
    assert [segment.setup_name for segment in report.segments] == ["account-a-setup"]
