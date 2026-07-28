import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ToolExecutionAudit
from app.policy import PolicyViolation
from app.services.workspaces import RequestScope, validate_strategy_scope

ToolExecutor = Callable[[str, dict[str, Any]], str]


def _canonical_arguments(arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    encoded = jsonable_encoder(arguments)
    serialized = json.dumps(
        encoded,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return encoded, hashlib.sha256(serialized.encode()).hexdigest()


def record_direct_cli_confirmation(
    db: Session,
    *,
    scope: RequestScope,
    action: str,
    arguments: dict[str, Any],
) -> ToolExecutionAudit:
    """Durably record what a human confirmed before a direct CLI mutation."""
    encoded_arguments, arguments_hash = _canonical_arguments(arguments)
    now = datetime.now(UTC)
    audit = ToolExecutionAudit(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        request_id=uuid.uuid4(),
        tool_name=action,
        arguments_hash=arguments_hash,
        arguments=encoded_arguments,
        status="confirmed",
        confirmed_at=now,
    )
    db.add(audit)
    db.commit()
    db.refresh(audit)
    return audit


def complete_mutation_audit(
    db: Session,
    audit_id: uuid.UUID,
    *,
    scope: RequestScope,
    error: BaseException | None = None,
    result_text: str | None = None,
) -> None:
    """Durably finish an audit after the mutation transaction has settled."""
    audit = db.scalar(
        select(ToolExecutionAudit).where(
            ToolExecutionAudit.id == audit_id,
            ToolExecutionAudit.workspace_id == scope.workspace_id,
            ToolExecutionAudit.account_id == scope.account_id,
        )
    )
    if audit is None:
        raise RuntimeError("mutation audit disappeared during execution")
    audit.status = "failed" if error is not None else "succeeded"
    audit.failure_type = type(error).__name__ if error is not None else None
    audit.completed_at = datetime.now(UTC)
    if result_text is not None:
        audit.result_text = result_text
        audit.result_hash = hashlib.sha256(result_text.encode()).hexdigest()
    db.commit()


class AuditedToolExecutor:
    """Persist mutation lifecycle and make identical calls idempotent per request."""

    def __init__(
        self,
        db: Session,
        execute: ToolExecutor,
        metadata: dict[str, dict[str, bool]],
        *,
        scope: RequestScope,
        request_id: uuid.UUID,
        conversation_session_id: uuid.UUID | None = None,
        user_turn_id: uuid.UUID | None = None,
        playbook_version_id: uuid.UUID | None = None,
    ) -> None:
        validate_strategy_scope(db, scope, playbook_version_id)
        self.db = db
        self.execute = execute
        self.metadata = metadata
        self.scope = scope
        self.request_id = request_id
        self.conversation_session_id = conversation_session_id
        self.user_turn_id = user_turn_id
        self.playbook_version_id = playbook_version_id
        self.enabled = isinstance(db, Session)
        self.succeeded = 0
        self.failed = 0
        self.declined = 0

    def __call__(self, name: str, arguments: dict[str, Any]) -> str:
        values = self.metadata.get(name)
        if values is None or not values["mutating"] or not self.enabled:
            return self.execute(name, arguments)

        encoded_arguments, arguments_hash = _canonical_arguments(arguments)
        existing = self._find(name, arguments_hash)
        if existing is not None:
            return self._replay(existing)

        audit = ToolExecutionAudit(
            workspace_id=self.scope.workspace_id,
            account_id=self.scope.account_id,
            conversation_session_id=self.conversation_session_id,
            user_turn_id=self.user_turn_id,
            playbook_version_id=self.playbook_version_id,
            request_id=self.request_id,
            tool_name=name,
            arguments_hash=arguments_hash,
            arguments=encoded_arguments,
            status="pending",
        )
        self.db.add(audit)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self._find(name, arguments_hash)
            if existing is None:
                raise
            return self._replay(existing)

        try:
            result = self.execute(name, arguments)
        except Exception as exc:
            self.db.rollback()
            current = self._get(audit.id)
            declined = (
                isinstance(exc, PolicyViolation)
                and str(exc) == "trader declined mutation"
            )
            current.status = "declined" if declined else "failed"
            current.failure_type = type(exc).__name__
            current.confirmed_at = None if declined else datetime.now(UTC)
            current.completed_at = datetime.now(UTC)
            self.db.commit()
            if declined:
                self.declined += 1
            else:
                self.failed += 1
            raise

        current = self._get(audit.id)
        current.status = "succeeded"
        current.confirmed_at = datetime.now(UTC)
        current.completed_at = datetime.now(UTC)
        current.result_text = result
        current.result_hash = hashlib.sha256(result.encode()).hexdigest()
        self.db.commit()
        self.succeeded += 1
        return result

    def _find(self, name: str, arguments_hash: str) -> ToolExecutionAudit | None:
        return self.db.scalar(
            select(ToolExecutionAudit).where(
                ToolExecutionAudit.workspace_id == self.scope.workspace_id,
                ToolExecutionAudit.account_id == self.scope.account_id,
                ToolExecutionAudit.request_id == self.request_id,
                ToolExecutionAudit.tool_name == name,
                ToolExecutionAudit.arguments_hash == arguments_hash,
            )
        )

    def _get(self, audit_id: uuid.UUID) -> ToolExecutionAudit:
        audit = self.db.scalar(
            select(ToolExecutionAudit).where(
                ToolExecutionAudit.workspace_id == self.scope.workspace_id,
                ToolExecutionAudit.account_id == self.scope.account_id,
                ToolExecutionAudit.id == audit_id,
            )
        )
        if audit is None:
            raise RuntimeError("mutation audit disappeared during execution")
        return audit

    def _replay(self, audit: ToolExecutionAudit) -> str:
        if audit.status == "succeeded" and audit.result_text is not None:
            self.succeeded += 1
            return audit.result_text
        if audit.status == "declined":
            self.declined += 1
            raise PolicyViolation("trader declined mutation")
        self.failed += 1
        raise PolicyViolation(
            "this mutation was already attempted for the current request; "
            "start a new request after reviewing its audit record"
        )
