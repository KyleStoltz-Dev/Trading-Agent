import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.main as main_module
from app.config import Settings
from app.models import AnalysisRun, EvidenceItem, Observation
from app.schemas import ChartAnalysis, PlaybookCheck
from app.services.evidence import record_chart_analysis


def test_api_authentication_and_mutation_confirmation_fail_closed(monkeypatch) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://ignored",
        trading_agent_api_key="x" * 32,
        openai_api_key=None,
        anthropic_api_key=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as missing:
        main_module.require_api_key(None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as wrong:
        main_module.require_api_key("y" * 32)
    assert wrong.value.status_code == 401

    main_module.require_api_key("x" * 32)
    with pytest.raises(HTTPException) as unconfirmed:
        main_module.require_trader_confirmation(None)
    assert unconfirmed.value.status_code == 428
    main_module.require_trader_confirmation("confirmed")


def test_short_api_key_is_rejected_even_when_it_matches(monkeypatch) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://ignored",
        trading_agent_api_key="short",
        openai_api_key=None,
        anthropic_api_key=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as rejected:
        main_module.require_api_key("short")
    assert rejected.value.status_code == 401


def test_chart_evidence_is_content_addressed_private_and_auditable(
    db_session, tmp_path
) -> None:
    analysis = ChartAnalysis(
        visible_facts=["Price reclaimed the marked low."],
        unreadable_or_missing=["Broker spread is not shown."],
        context_hypotheses=["The range may be reaccumulation."],
        trigger_hypotheses=["The reclaim may be a spring confirmation."],
        playbook_checks=[
            PlaybookCheck(
                check="liquidity_sweep",
                status="met",
                evidence=["Low was breached and reclaimed."],
            )
        ],
        risk_questions=["Where is invalidation?"],
        management_questions=["Is partial profit predefined?"],
        disclaimer="Decision support only.",
    )
    provider = SimpleNamespace(name="test-provider", model="test-model")
    image = b"\x89PNG\r\n\x1a\nsynthetic-test"

    evidence, run = record_chart_analysis(
        db_session,
        image_bytes=image,
        content_type="image/png",
        evidence_directory=tmp_path / "evidence",
        analysis=analysis,
        provider=provider,
        policy_hash="a" * 64,
        prompt="separate facts from hypotheses",
        source="test",
        market_time=datetime(2026, 7, 23, 15, 0, tzinfo=UTC),
        instrument="XAUUSD",
        venue="OANDA",
        timeframe="M5",
    )
    duplicate, second_run = record_chart_analysis(
        db_session,
        image_bytes=image,
        content_type="image/png",
        evidence_directory=tmp_path / "evidence",
        analysis=analysis,
        provider=provider,
        policy_hash="a" * 64,
        prompt="separate facts from hypotheses",
        source="test",
        market_time=datetime(2026, 7, 23, 15, 0, tzinfo=UTC),
        instrument="XAUUSD",
        venue="OANDA",
        timeframe="M5",
    )

    evidence_path = tmp_path / "evidence" / evidence.sha256[:2] / f"{evidence.sha256}.png"
    mode = stat.S_IMODE(evidence_path.stat().st_mode)
    evidence_count = db_session.scalar(select(func.count()).select_from(EvidenceItem))
    run_count = db_session.scalar(select(func.count()).select_from(AnalysisRun))
    observations = list(
        db_session.scalars(
            select(Observation).where(Observation.evidence_id == evidence.id)
        )
    )

    assert duplicate.id == evidence.id
    assert second_run.id != run.id
    assert evidence_count == 1
    assert run_count == 2
    assert mode == 0o600
    assert run.input_hash == evidence.sha256
    assert run.policy_hash == "a" * 64
    assert {item.kind for item in observations} == {"fact", "hypothesis"}
