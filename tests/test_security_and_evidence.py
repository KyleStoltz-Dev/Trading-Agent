import asyncio
import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.requests import Request

import app.main as main_module
from app.config import Settings
from app.models import AnalysisRun, EvidenceItem, Observation
from app.schemas import ChartAnalysis, PlaybookCheck
from app.services.evidence import record_chart_analysis, store_evidence_file


def _request(
    body: bytes,
    store: main_module.ConfirmationStore,
    *,
    path: str = "/api/trades",
) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "app": SimpleNamespace(
                state=SimpleNamespace(confirmations=store),
            ),
        },
        receive,
    )


def test_api_authentication_fail_closed(monkeypatch) -> None:
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


def test_mutation_confirmation_is_one_time_and_request_bound() -> None:
    body = b'{"instrument":"XAUUSD"}'
    store = main_module.ConfirmationStore(ttl_seconds=60)
    token = store.issue(
        method="POST",
        path="/api/trades",
        body_sha256=hashlib.sha256(body).hexdigest(),
    )
    request = _request(body, store)

    asyncio.run(main_module.require_trader_confirmation(request, token))

    with pytest.raises(HTTPException) as replay:
        asyncio.run(main_module.require_trader_confirmation(request, token))
    assert replay.value.status_code == 428
    assert "already used" in replay.value.detail


def test_confirmation_token_is_random_bounded_and_atomically_consumed() -> None:
    body_hash = hashlib.sha256(b"{}").hexdigest()
    store = main_module.ConfirmationStore(ttl_seconds=60)
    tokens = {
        store.issue(
            method="POST",
            path="/api/trades",
            body_sha256=body_hash,
        )
        for _ in range(32)
    }
    assert len(tokens) == 32
    assert all(len(token) == 43 for token in tokens)

    token = next(iter(tokens))
    with ThreadPoolExecutor(max_workers=16) as executor:
        consumed = list(
            executor.map(
                lambda _: store.consume(
                    token,
                    method="POST",
                    path="/api/trades",
                    body_sha256=body_hash,
                ),
                range(32),
            )
        )
    assert consumed.count(True) == 1
    assert store.consume(
        "x" * 10_000,
        method="POST",
        path="/api/trades",
        body_sha256=body_hash,
    ) is False


def test_authenticated_client_can_issue_short_lived_bound_challenge(
    monkeypatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://ignored",
        database_auto_migrate=False,
        trading_agent_api_key="x" * 32,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    body_sha256 = hashlib.sha256(b"{}").hexdigest()

    with TestClient(main_module.app) as client:
        response = client.post(
            "/api/confirmations/challenge",
            headers={"X-API-Key": "x" * 32},
            json={
                "method": "POST",
                "path": "/api/trades",
                "body_sha256": body_sha256,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["path"] == "/api/trades"
    assert payload["body_sha256"] == body_sha256
    assert payload["token"] != "confirmed"


def test_api_rejects_oversized_body_before_parsing_and_authenticates_first(
    monkeypatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://ignored",
        database_auto_migrate=False,
        trading_agent_api_key="x" * 32,
        api_max_request_bytes=1024,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    oversized = b"x" * 1025

    with TestClient(main_module.app) as client:
        unauthenticated = client.post("/api/trades", content=oversized)
        authenticated = client.post(
            "/api/trades",
            headers={"X-API-Key": "x" * 32},
            content=oversized,
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "valid API key required"}
    assert authenticated.status_code == 413
    assert authenticated.json() == {
        "detail": "API request body exceeds configured limit"
    }


def test_multipart_confirmation_binds_exact_boundary_and_bytes() -> None:
    first = (
        b"--boundary-a\r\nContent-Disposition: form-data; name=\"context\"\r\n\r\n"
        b"private context\r\n--boundary-a--\r\n"
    )
    second = first.replace(b"boundary-a", b"boundary-b")
    store = main_module.ConfirmationStore(ttl_seconds=60)
    token = store.issue(
        method="POST",
        path="/api/charts/analyze",
        body_sha256=hashlib.sha256(first).hexdigest(),
    )

    assert not store.consume(
        token,
        method="POST",
        path="/api/charts/analyze",
        body_sha256=hashlib.sha256(second).hexdigest(),
    )
    assert not store.consume(
        token,
        method="POST",
        path="/api/charts/analyze",
        body_sha256=hashlib.sha256(first).hexdigest(),
    )


def test_mutation_confirmation_rejects_body_substitution_and_consumes_token() -> None:
    approved_body = b'{"risk_percent":"0.5"}'
    substituted_body = b'{"risk_percent":"5"}'
    store = main_module.ConfirmationStore(ttl_seconds=60)
    token = store.issue(
        method="POST",
        path="/api/trades",
        body_sha256=hashlib.sha256(approved_body).hexdigest(),
    )

    with pytest.raises(HTTPException) as substituted:
        asyncio.run(
            main_module.require_trader_confirmation(
                _request(substituted_body, store),
                token,
            )
        )
    assert substituted.value.status_code == 428

    with pytest.raises(HTTPException):
        asyncio.run(
            main_module.require_trader_confirmation(
                _request(approved_body, store),
                token,
            )
        )


def test_mutation_confirmation_expires() -> None:
    clock = iter((100.0, 111.0))
    body = b"{}"
    store = main_module.ConfirmationStore(
        ttl_seconds=10,
        clock=lambda: next(clock),
    )
    token = store.issue(
        method="POST",
        path="/api/trades",
        body_sha256=hashlib.sha256(body).hexdigest(),
    )

    assert not store.consume(
        token,
        method="POST",
        path="/api/trades",
        body_sha256=hashlib.sha256(body).hexdigest(),
    )


def test_strategy_facing_api_requires_explicit_immutable_scope() -> None:
    db = Mock()
    with pytest.raises(HTTPException) as missing:
        main_module.require_strategy_version(db, None)
    assert missing.value.status_code == 428
    identifier = main_module.uuid.UUID("11111111-1111-1111-1111-111111111111")
    db.get.return_value = SimpleNamespace(id=identifier)
    strategy_version = main_module.require_strategy_version(db, identifier)
    assert str(strategy_version) == "11111111-1111-1111-1111-111111111111"

    db.get.return_value = None
    with pytest.raises(HTTPException) as unknown:
        main_module.require_strategy_version(db, identifier)
    assert unknown.value.status_code == 404
    assert str(identifier) not in unknown.value.detail


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


def test_evidence_storage_rejects_symlinked_or_tampered_targets(tmp_path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store_evidence_file(b"image", "image/png", linked)

    path, _ = store_evidence_file(b"image", "image/png", tmp_path / "evidence")
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        store_evidence_file(b"image", "image/png", tmp_path / "evidence")


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
    evidence_count = db_session.scalar(
        select(func.count())
        .select_from(EvidenceItem)
        .where(EvidenceItem.sha256 == evidence.sha256)
    )
    run_count = db_session.scalar(
        select(func.count())
        .select_from(AnalysisRun)
        .where(AnalysisRun.evidence_id == evidence.id)
    )
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
