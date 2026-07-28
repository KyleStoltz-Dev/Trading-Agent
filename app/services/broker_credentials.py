"""Transactional metadata workflow for per-account broker credentials."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BrokerConnection, SecurityAuditEvent
from app.services.secrets import (
    new_secret_reference,
    remove_broker_secret,
    store_broker_secret,
)
from app.services.workspaces import RequestScope, validate_scope


@dataclass(frozen=True)
class CredentialChangeResult:
    connection: BrokerConnection
    cleanup_pending: bool = False


def _audit_cleanup_failure(
    db: Session,
    *,
    scope: RequestScope,
    actor: str,
    provider: str,
    reference: str,
    error: Exception,
) -> None:
    db.add(
        SecurityAuditEvent(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            action="credential_cleanup_failed",
            actor=actor,
            secret_reference=reference,
            metadata_json={
                "provider": provider,
                "error_type": type(error).__name__,
                "retry_required": True,
            },
        )
    )
    db.commit()


def rotate_broker_credential(
    db: Session,
    settings: Settings,
    *,
    scope: RequestScope,
    provider: str,
    token: str,
    actor: str,
) -> CredentialChangeResult:
    validate_scope(db, scope)
    connection = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.workspace_id == scope.workspace_id,
            BrokerConnection.account_id == scope.account_id,
            BrokerConnection.provider == provider,
        )
    )
    if connection is None:
        raise LookupError("broker connection was not found for the selected account")
    new_reference = new_secret_reference(settings)
    store_broker_secret(
        settings,
        reference=new_reference,
        provider=provider,
        token=token,
    )
    old_reference = connection.config_reference
    action = "credential_rotated" if old_reference else "credential_created"
    try:
        connection.config_reference = new_reference
        connection.status = "configured"
        db.add(
            SecurityAuditEvent(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                action=action,
                actor=actor,
                secret_reference=new_reference,
                metadata_json={"provider": provider},
            )
        )
        db.commit()
    except BaseException:
        db.rollback()
        remove_broker_secret(settings, new_reference)
        raise
    cleanup_pending = False
    if old_reference and not old_reference.startswith("env:"):
        try:
            remove_broker_secret(settings, old_reference)
        except Exception as exc:
            cleanup_pending = True
            _audit_cleanup_failure(
                db,
                scope=scope,
                actor=actor,
                provider=provider,
                reference=old_reference,
                error=exc,
            )
    db.refresh(connection)
    return CredentialChangeResult(connection, cleanup_pending)


def remove_broker_credential(
    db: Session,
    settings: Settings,
    *,
    scope: RequestScope,
    provider: str,
    actor: str,
) -> CredentialChangeResult:
    validate_scope(db, scope)
    connection = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.workspace_id == scope.workspace_id,
            BrokerConnection.account_id == scope.account_id,
            BrokerConnection.provider == provider,
        )
    )
    if connection is None:
        raise LookupError("broker connection was not found for the selected account")
    reference = connection.config_reference
    connection.status = "disabled"
    db.add(
        SecurityAuditEvent(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            action="credential_removed",
            actor=actor,
            secret_reference=reference,
            metadata_json={"provider": provider},
        )
    )
    db.commit()
    cleanup_pending = False
    if reference and not reference.startswith("env:"):
        try:
            remove_broker_secret(settings, reference)
        except Exception as exc:
            cleanup_pending = True
            _audit_cleanup_failure(
                db,
                scope=scope,
                actor=actor,
                provider=provider,
                reference=reference,
                error=exc,
            )
        else:
            connection.config_reference = None
            db.commit()
    else:
        connection.config_reference = None
        db.commit()
    db.refresh(connection)
    return CredentialChangeResult(connection, cleanup_pending)


def retry_broker_secret_cleanup(
    db: Session,
    settings: Settings,
    *,
    scope: RequestScope,
    audit_event_id: uuid.UUID,
    actor: str,
) -> None:
    """Retry one explicitly audited orphan/disabled vault deletion."""
    validate_scope(db, scope)
    event = db.scalar(
        select(SecurityAuditEvent).where(
            SecurityAuditEvent.id == audit_event_id,
            SecurityAuditEvent.workspace_id == scope.workspace_id,
            SecurityAuditEvent.account_id == scope.account_id,
            SecurityAuditEvent.action == "credential_cleanup_failed",
        )
    )
    if event is None or not event.secret_reference:
        raise LookupError("retryable credential cleanup event was not found")
    connections = list(
        db.scalars(
            select(BrokerConnection).where(
                BrokerConnection.workspace_id == scope.workspace_id,
                BrokerConnection.account_id == scope.account_id,
                BrokerConnection.config_reference == event.secret_reference,
            )
        )
    )
    if any(connection.status != "disabled" for connection in connections):
        raise RuntimeError("refusing to delete a credential used by an active connection")
    remove_broker_secret(settings, event.secret_reference)
    for connection in connections:
        connection.config_reference = None
    db.add(
        SecurityAuditEvent(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            action="credential_removed",
            actor=actor,
            secret_reference=event.secret_reference,
            metadata_json={
                "provider": event.metadata_json.get("provider"),
                "cleanup_retry_for": str(event.id),
            },
        )
    )
    db.commit()
