import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import db as db_module
from app import main as main_module
from app.config import Settings
from app.db import SessionLocal, get_db, verify_hosted_rls
from app.models import (
    ApiPrincipal,
    BrokerConnection,
    SecurityAuditEvent,
)
from app.services.broker_credentials import (
    remove_broker_credential,
    retry_broker_secret_cleanup,
    rotate_broker_credential,
)
from app.services.principals import (
    authenticate_principal,
    create_principal,
    grant_principal,
    revoke_principal_grant,
    rotate_principal_token,
)
from app.services.secrets import (
    BrokerCredentials,
    resolve_broker_credentials,
)


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}
        self.fail_delete = False

    def get(self, reference: str):
        return self.values.get(reference)

    def put(self, reference: str, values):
        self.values[reference] = dict(values)

    def delete(self, reference: str) -> None:
        if self.fail_delete:
            raise RuntimeError("vault unavailable")
        self.values.pop(reference, None)


def _external_settings() -> Settings:
    return Settings(
        broker_secret_backend="external",
        broker_external_secret_backend="tests.fake:backend",
    )


def test_account_secret_resolution_never_uses_another_reference(monkeypatch) -> None:
    backend = MemorySecrets()
    backend.values["external:broker/one"] = {"token": "first"}
    backend.values["external:broker/two"] = {"token": "second"}
    monkeypatch.setattr("app.services.secrets.secret_backend", lambda settings: backend)
    settings = _external_settings()

    first = resolve_broker_credentials(
        settings,
        provider="oanda-v20",
        reference="external:broker/one",
    )
    second = resolve_broker_credentials(
        settings,
        provider="oanda-v20",
        reference="external:broker/two",
    )

    assert first == BrokerCredentials(token="first")
    assert second == BrokerCredentials(token="second")


def test_credential_rotation_and_removal_are_audited_without_plaintext(
    monkeypatch,
    db_session,
    request_scope,
) -> None:
    backend = MemorySecrets()
    monkeypatch.setattr("app.services.secrets.secret_backend", lambda settings: backend)
    monkeypatch.setattr(
        "app.services.broker_credentials.new_secret_reference",
        lambda settings: "external:broker/new",
    )
    settings = _external_settings()
    connection = BrokerConnection(
        workspace_id=request_scope.workspace_id,
        account_id=request_scope.account_id,
        provider="oanda-v20",
        environment="practice",
        config_reference=None,
    )
    db_session.add(connection)
    db_session.commit()

    changed = rotate_broker_credential(
        db_session,
        settings,
        scope=request_scope,
        provider="oanda-v20",
        token="never-store-this-token",
        actor="test",
    )
    assert changed.connection.config_reference == "external:broker/new"
    assert "never-store-this-token" not in str(
        list(db_session.scalars(select(SecurityAuditEvent)))
    )

    removed = remove_broker_credential(
        db_session,
        settings,
        scope=request_scope,
        provider="oanda-v20",
        actor="test",
    )
    assert removed.connection.status == "disabled"
    assert removed.connection.config_reference is None
    assert [event.action for event in db_session.scalars(select(SecurityAuditEvent))] == [
        "credential_created",
        "credential_removed",
    ]


def test_vault_cleanup_failure_is_truthful_and_retryable(
    monkeypatch,
    db_session,
    request_scope,
) -> None:
    backend = MemorySecrets()
    backend.values["external:broker/existing"] = {"token": "x" * 40}
    backend.fail_delete = True
    monkeypatch.setattr("app.services.secrets.secret_backend", lambda settings: backend)
    settings = _external_settings()
    connection = BrokerConnection(
        workspace_id=request_scope.workspace_id,
        account_id=request_scope.account_id,
        provider="oanda-v20",
        environment="practice",
        config_reference="external:broker/existing",
    )
    db_session.add(connection)
    db_session.commit()

    result = remove_broker_credential(
        db_session,
        settings,
        scope=request_scope,
        provider="oanda-v20",
        actor="test",
    )

    assert result.cleanup_pending is True
    assert result.connection.status == "disabled"
    assert result.connection.config_reference == "external:broker/existing"
    cleanup = db_session.scalar(
        select(SecurityAuditEvent).where(
            SecurityAuditEvent.action == "credential_cleanup_failed"
        )
    )
    assert cleanup is not None
    assert cleanup.metadata_json["retry_required"] is True

    backend.fail_delete = False
    retry_broker_secret_cleanup(
        db_session,
        settings,
        scope=request_scope,
        audit_event_id=cleanup.id,
        actor="test",
    )
    db_session.refresh(connection)
    assert connection.config_reference is None
    assert "external:broker/existing" not in backend.values


def test_principal_token_grant_is_exactly_account_scoped(
    db_session,
    request_scope,
) -> None:
    principal, token = create_principal(db_session, "trader@example.test")
    grant_principal(
        db_session,
        principal_id=principal.id,
        scope=request_scope,
        role="trader",
        actor="test",
    )

    authorized = authenticate_principal(
        db_session,
        bearer_token=token,
        scope=request_scope,
    )
    assert authorized is not None
    assert authorized.role == "trader"
    assert db_session.scalar(
        select(ApiPrincipal).where(ApiPrincipal.token_sha256 == token)
    ) is None
    assert (
        authenticate_principal(
            db_session,
            bearer_token=token,
            scope=type(request_scope)(
                request_scope.workspace_id,
                uuid.uuid4(),
            ),
        )
        is None
    )

    replacement = rotate_principal_token(
        db_session,
        principal.id,
        scope=request_scope,
        actor="test",
    )
    assert (
        authenticate_principal(
            db_session,
            bearer_token=token,
            scope=request_scope,
        )
        is None
    )
    assert (
        authenticate_principal(
            db_session,
            bearer_token=replacement,
            scope=request_scope,
        )
        is not None
    )
    revoke_principal_grant(
        db_session,
        principal_id=principal.id,
        scope=request_scope,
        actor="test",
    )
    assert (
        authenticate_principal(
            db_session,
            bearer_token=replacement,
            scope=request_scope,
        )
        is None
    )


def test_tenant_gucs_reapply_after_commit(request_scope) -> None:
    with SessionLocal() as session:
        session.info["tenant_scope"] = request_scope
        first = session.execute(
            text(
                "SELECT current_setting('app.workspace_id'), "
                "current_setting('app.account_id')"
            )
        ).one()
        session.commit()
        second = session.execute(
            text(
                "SELECT current_setting('app.workspace_id'), "
                "current_setting('app.account_id')"
            )
        ).one()

    assert first == second == (
        str(request_scope.workspace_id),
        str(request_scope.account_id),
    )


def test_rls_policies_cover_all_workspace_owned_tables(db_session: Session) -> None:
    missing = db_session.execute(
        text(
            """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN information_schema.columns col
                ON col.table_schema = n.nspname AND col.table_name = c.relname
             WHERE n.nspname = 'public'
               AND col.column_name = 'workspace_id'
               AND c.relname <> 'api_principal_grants'
             GROUP BY c.oid, c.relname, c.relrowsecurity
            HAVING NOT c.relrowsecurity OR NOT EXISTS (
                SELECT 1 FROM pg_policy p
                 WHERE p.polrelid = c.oid AND p.polname = 'tenant_scope'
            )
            """
        )
    ).scalars().all()
    assert missing == []


def test_hosted_startup_rejects_table_owner_runtime_role(db_session: Session) -> None:
    with pytest.raises(RuntimeError, match="not enforceable"):
        verify_hosted_rls()


def test_hosted_api_bearer_is_bound_to_exact_account(
    monkeypatch,
    db_session,
    request_scope,
) -> None:
    principal, token = create_principal(db_session, "hosted-reader@example.test")
    grant_principal(
        db_session,
        principal_id=principal.id,
        scope=request_scope,
        role="reader",
        actor="test",
    )
    settings = Settings.model_construct(
        deployment_mode="hosted-multi-user",
        database_auto_migrate=False,
        api_requests_per_minute=120,
        tradingview_webhook_enabled=False,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "verify_hosted_rls", lambda: None)
    monkeypatch.setattr(main_module, "validate_secret_backend", lambda settings: None)

    class AuthenticationSession:
        def __enter__(self):
            return db_session

        def __exit__(self, *unused):
            return False

    monkeypatch.setattr(main_module, "SessionLocal", AuthenticationSession)

    def database_override():
        yield db_session

    main_module.app.dependency_overrides[get_db] = database_override
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Workspace-ID": str(request_scope.workspace_id),
        "X-Account-ID": str(request_scope.account_id),
    }
    try:
        with TestClient(main_module.app) as client:
            accepted = client.get(
                "/api/integrations/tradingview/alerts",
                headers=headers,
            )
            rejected = client.get(
                "/api/integrations/tradingview/alerts",
                headers={**headers, "X-Account-ID": str(uuid.uuid4())},
            )
    finally:
        main_module.app.dependency_overrides.clear()

    assert accepted.status_code == 200
    assert rejected.status_code == 403


def test_confirmation_token_cannot_cross_principal(request_scope) -> None:
    first = uuid.uuid4()
    store = main_module.ConfirmationStore(ttl_seconds=60)
    token = store.issue(
        method="POST",
        path="/api/trades",
        body_sha256="a" * 64,
        scope=request_scope,
        principal_id=first,
    )

    assert (
        store.consume(
            token,
            method="POST",
            path="/api/trades",
            body_sha256="a" * 64,
            scope=request_scope,
            principal_id=uuid.uuid4(),
        )
        is False
    )
