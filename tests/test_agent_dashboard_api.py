import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main_module
from app.config import Settings
from app.schemas import StrategySummary
from app.services.workspaces import RequestScope


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://ignored:ignored@localhost/ignored",
        database_auto_migrate=False,
        trading_agent_api_key="x" * 32,
        model_provider="ollama",
    )


def _capture_audits(monkeypatch) -> list[dict]:
    captured: list[dict] = []

    @contextmanager
    def capture(_db, **values):
        captured.append(values)
        yield

    monkeypatch.setattr(main_module, "audit_api_mutation", capture)
    return captured


def test_agent_context_and_model_catalog_are_available_to_dashboard(
    monkeypatch,
) -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    monkeypatch.setattr(main_module, "get_settings", _settings)
    monkeypatch.setattr(main_module, "resolve_current_scope", lambda *_args, **_kwargs: scope)
    monkeypatch.setattr(
        main_module,
        "selectable_agent_models",
        lambda _settings: (
            SimpleNamespace(
                provider="ollama",
                model="qwen3.5:9b",
                label="Local · qwen3.5:9b",
                location="local",
                available=True,
                selected=True,
            ),
        ),
    )
    main_module.app.dependency_overrides[main_module.get_db] = lambda: Mock()

    try:
        with TestClient(main_module.app) as client:
            headers = {"X-API-Key": "x" * 32}
            context = client.get("/api/agent/context", headers=headers)
            models = client.get("/api/agent/models", headers=headers)
    finally:
        main_module.app.dependency_overrides.clear()

    assert context.status_code == 200
    assert context.json() == {
        "workspace_id": str(scope.workspace_id),
        "account_id": str(scope.account_id),
        "broker_provider": "none",
        "news_provider": "none",
    }
    assert models.status_code == 200
    assert models.json()[0]["model"] == "qwen3.5:9b"


def test_launcher_fragment_is_exchanged_once_without_root_auto_auth(monkeypatch) -> None:
    bootstrap_token = "b" * 43
    settings = _settings().model_copy(
        update={
            "trading_dashboard_autoconnect": True,
            "trading_dashboard_bootstrap_token": SecretStr(bootstrap_token),
        }
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "selectable_agent_models", lambda _settings: ())

    with TestClient(main_module.app) as client:
        index = client.get("/")
        models_before = client.get("/api/agent/models")
        session = client.post(
            "/api/dashboard/session",
            headers={"X-API-Key": bootstrap_token},
        )
        models_after = client.get("/api/agent/models")
        replay = client.post(
            "/api/dashboard/session",
            headers={"X-API-Key": bootstrap_token},
        )

    assert index.status_code == 200
    assert "set-cookie" not in index.headers
    assert models_before.status_code == 401
    assert session.status_code == 204
    assert "HttpOnly" in session.headers["set-cookie"]
    assert "SameSite=strict" in session.headers["set-cookie"]
    assert models_after.status_code == 200
    assert replay.status_code == 401


def test_dashboard_bootstrap_rate_limit_cannot_be_evaded_with_rotating_tokens(
    monkeypatch,
) -> None:
    settings = _settings().model_copy(
        update={
            "api_requests_per_minute": 1,
            "trading_dashboard_autoconnect": True,
            "trading_dashboard_bootstrap_token": SecretStr("b" * 43),
        }
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        first = client.post(
            "/api/dashboard/session",
            headers={"X-API-Key": "a" * 43},
        )
        rotated = client.post(
            "/api/dashboard/session",
            headers={"X-API-Key": "c" * 43},
        )

    assert first.status_code == 401
    assert rotated.status_code == 429


def test_pippy_can_connect_cloud_brain_without_exposing_key(monkeypatch) -> None:
    settings = _settings()
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    audits = _capture_audits(monkeypatch)
    saved: dict[str, str] = {}
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module,
        "model_api_key_configured",
        lambda _settings, *, provider: provider == "openai",
    )
    monkeypatch.setattr(
        main_module,
        "store_model_api_key",
        lambda _settings, *, provider, api_key: saved.update(
            provider=provider,
            api_key=api_key,
        ),
    )
    main_module.app.dependency_overrides[main_module.get_db] = lambda: Mock()
    main_module.app.dependency_overrides[main_module.require_request_scope] = lambda: scope
    main_module.app.dependency_overrides[main_module.require_trader_confirmation] = (
        lambda: None
    )

    try:
        with TestClient(main_module.app) as client:
            providers = client.get(
                "/api/agent/providers",
                headers={"X-API-Key": "x" * 32},
            )
            connected = client.post(
                "/api/agent/providers/anthropic/credentials",
                headers={"X-API-Key": "x" * 32},
                json={"api_key": "secret-claude-key"},
            )
    finally:
        main_module.app.dependency_overrides.clear()

    assert providers.status_code == 200
    assert providers.json()[1]["configured"] is True
    assert providers.json()[2]["configured"] is False
    assert connected.status_code == 200
    assert connected.json()["provider"] == "anthropic"
    assert "api_key" not in connected.json()
    assert saved == {"provider": "anthropic", "api_key": "secret-claude-key"}
    assert audits == [
        {
            "scope": scope,
            "action": "configure_agent_provider",
            "arguments": {"provider": "anthropic"},
        }
    ]


def test_provider_credentials_require_exact_human_confirmation(monkeypatch) -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    monkeypatch.setattr(main_module, "get_settings", _settings)
    main_module.app.dependency_overrides[main_module.get_db] = lambda: Mock()
    main_module.app.dependency_overrides[main_module.require_request_scope] = lambda: scope

    try:
        with TestClient(main_module.app) as client:
            response = client.post(
                "/api/agent/providers/openai/credentials",
                headers={"X-API-Key": "x" * 32},
                json={"api_key": "secret-openai-key"},
            )
    finally:
        main_module.app.dependency_overrides.clear()

    assert response.status_code == 428


def test_pippy_realtime_secret_uses_vault_key_without_exposing_it(monkeypatch) -> None:
    settings = _settings().model_copy(
        update={"openai_realtime_model": "gpt-realtime-2.1-mini"}
    )
    captured: dict[str, str] = {}
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module,
        "resolve_model_credentials",
        lambda _settings, *, provider: SimpleNamespace(api_key="vault-openai-key"),
    )
    monkeypatch.setattr(
        main_module,
        "create_realtime_client_secret",
        lambda **kwargs: captured.update(kwargs) or {
            "value": "short-lived-key",
            "expires_at": 1234,
        },
    )

    with TestClient(main_module.app) as client:
        response = client.post(
            "/api/agent/realtime/client-secret",
            headers={"X-API-Key": "x" * 32},
            json={"voice": "marin"},
        )

    assert response.status_code == 200
    assert response.json() == {"value": "short-lived-key", "expires_at": 1234}
    assert captured["api_key"] == "vault-openai-key"
    assert captured["model"] == "gpt-realtime-2.1-mini"
    assert "vault-openai-key" not in response.text


def test_pippy_realtime_usage_is_forwarded_to_durable_store(monkeypatch) -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    session_id = uuid.uuid4()
    usage_id = uuid.uuid4()
    database = Mock()
    captured = {}
    audits = _capture_audits(monkeypatch)
    monkeypatch.setattr(main_module, "get_settings", _settings)
    monkeypatch.setattr(
        main_module,
        "record_realtime_usage",
        lambda _db, **kwargs: captured.update(kwargs)
        or SimpleNamespace(id=usage_id, created_at="2026-09-08T12:00:00Z", **kwargs),
    )
    main_module.app.dependency_overrides[main_module.get_db] = lambda: database
    main_module.app.dependency_overrides[main_module.require_request_scope] = lambda: scope
    payload = {
        "response_id": "resp_123",
        "model": "gpt-realtime-2.1-mini",
        "input_text_tokens": 12,
        "input_audio_tokens": 30,
        "cached_text_tokens": 8,
        "cached_audio_tokens": 10,
        "output_text_tokens": 6,
        "output_audio_tokens": 20,
        "estimated_cost_usd": "0.001234",
    }
    try:
        with TestClient(main_module.app) as client:
            response = client.post(
                f"/api/agent/sessions/{session_id}/realtime-usage",
                headers={
                    "X-API-Key": "x" * 32,
                    "X-Workspace-ID": str(scope.workspace_id),
                    "X-Account-ID": str(scope.account_id),
                },
                json=payload,
            )
    finally:
        main_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["response_id"] == "resp_123"
    assert captured["scope"] == scope
    assert captured["session_id"] == session_id
    assert audits[0]["action"] == "record_realtime_usage"
    assert audits[0]["scope"] == scope


def test_dashboard_lists_exact_scoped_strategy_versions(monkeypatch) -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    version_id = uuid.uuid4()
    summary = StrategySummary(
        playbook_id=uuid.uuid4(),
        playbook_version_id=version_id,
        name="XAU discipline",
        description="Reviewed strategy",
        version=3,
        content_hash="a" * 64,
        sample_requirement=20,
    )
    monkeypatch.setattr(main_module, "get_settings", _settings)
    monkeypatch.setattr(
        main_module,
        "list_strategy_summaries",
        lambda _db, *, scope: [summary],
    )
    main_module.app.dependency_overrides[main_module.get_db] = lambda: Mock()
    main_module.app.dependency_overrides[main_module.require_request_scope] = lambda: scope

    try:
        with TestClient(main_module.app) as client:
            response = client.get("/api/strategies", headers={"X-API-Key": "x" * 32})
    finally:
        main_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["playbook_version_id"] == str(version_id)
    assert response.json()[0]["name"] == "XAU discipline"


def test_dashboard_agent_session_uses_gateway_and_preserves_read_only_boundary(
    monkeypatch,
) -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    session_id = uuid.uuid4()
    database = Mock()
    audits = _capture_audits(monkeypatch)
    monkeypatch.setattr(main_module, "get_settings", _settings)
    monkeypatch.setattr(
        main_module,
        "start_agent_session",
        lambda *_args, **_kwargs: SimpleNamespace(
            id=session_id,
            name="daily-dashboard",
            title="Dashboard trading desk",
        ),
    )
    monkeypatch.setattr(
        main_module,
        "run_agent_turn",
        lambda *_args, **_kwargs: SimpleNamespace(
            session_id=session_id,
            response="Broker evidence is unavailable; no values were inferred.",
            provider="ollama",
            model="qwen3.5:9b",
            mode="auto",
            input_tokens=8,
            output_tokens=6,
            references=(),
        ),
    )
    main_module.app.dependency_overrides[main_module.get_db] = lambda: database
    main_module.app.dependency_overrides[main_module.require_request_scope] = lambda: scope

    headers = {
        "X-API-Key": "x" * 32,
        "X-Workspace-ID": str(scope.workspace_id),
        "X-Account-ID": str(scope.account_id),
    }
    try:
        with TestClient(main_module.app) as client:
            created = client.post(
                "/api/agent/sessions",
                headers=headers,
                json={"title": "Dashboard trading desk"},
            )
            message = client.post(
                f"/api/agent/sessions/{session_id}/messages",
                headers=headers,
                json={
                    "message": "Give me a day-start brief.",
                    "provider": "ollama",
                    "model": "qwen3.5:9b",
                    "mode": "auto",
                },
            )
    finally:
        main_module.app.dependency_overrides.clear()

    assert created.status_code == 201
    assert created.json()["session_id"] == str(session_id)
    assert message.status_code == 200
    assert message.json()["response"].endswith("no values were inferred.")
    assert [item["action"] for item in audits] == [
        "start_agent_session",
        "run_agent_turn",
    ]
