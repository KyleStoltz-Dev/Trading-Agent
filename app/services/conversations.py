import re
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import LEGACY_UNASSIGNED_ACCOUNT_ID, LEGACY_WORKSPACE_ID
from app.models import ConversationSession, ConversationTurn
from app.services.workspaces import (
    RequestScope,
    validate_scope,
    validate_strategy_scope,
)

SESSION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _legacy_scope(scope: RequestScope | None) -> RequestScope:
    return scope or RequestScope(
        workspace_id=uuid.UUID(LEGACY_WORKSPACE_ID),
        account_id=uuid.UUID(LEGACY_UNASSIGNED_ACCOUNT_ID),
    )


def normalize_session_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized or len(normalized) > 80 or not SESSION_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "session name must contain letters or numbers and be at most 80 characters"
        )
    return normalized


def get_conversation_by_name(
    db: Session,
    name: str,
    *,
    scope: RequestScope | None = None,
) -> ConversationSession | None:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    return _get_conversation_by_name(db, name, scope=scope)


def _get_conversation_by_name(
    db: Session,
    name: str,
    *,
    scope: RequestScope,
) -> ConversationSession | None:
    return db.scalar(
        select(ConversationSession).where(
            ConversationSession.workspace_id == scope.workspace_id,
            ConversationSession.account_id == scope.account_id,
            ConversationSession.name == normalize_session_name(name)
        )
    )


def resolve_conversation(
    db: Session,
    reference: str,
    *,
    scope: RequestScope | None = None,
) -> ConversationSession | None:
    scope = _legacy_scope(scope)
    try:
        return get_conversation(db, uuid.UUID(reference), scope=scope)
    except ValueError:
        return get_conversation_by_name(db, reference, scope=scope)


def _available_daily_name(db: Session, *, scope: RequestScope) -> str:
    base = f"daily-{date.today().isoformat()}"
    if _get_conversation_by_name(db, base, scope=scope) is None:
        return base
    suffix = 2
    while _get_conversation_by_name(db, f"{base}-{suffix}", scope=scope) is not None:
        suffix += 1
    return f"{base}-{suffix}"


def create_conversation(
    db: Session,
    name: str | None = None,
    title: str = "Trading Agent session",
    *,
    scope: RequestScope | None = None,
) -> ConversationSession:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    session_name = (
        normalize_session_name(name)
        if name
        else _available_daily_name(db, scope=scope)
    )
    if _get_conversation_by_name(db, session_name, scope=scope):
        raise ValueError(f"session name already exists: {session_name}")
    conversation = ConversationSession(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        name=session_name,
        title=title,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def get_conversation(
    db: Session,
    session_id: uuid.UUID,
    *,
    scope: RequestScope | None = None,
) -> ConversationSession | None:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    return db.scalar(
        select(ConversationSession).where(
            ConversationSession.workspace_id == scope.workspace_id,
            ConversationSession.account_id == scope.account_id,
            ConversationSession.id == session_id,
        )
    )


def list_conversations(
    db: Session,
    limit: int = 20,
    *,
    scope: RequestScope | None = None,
) -> list[ConversationSession]:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    return list(
        db.scalars(
            select(ConversationSession)
            .where(
                ConversationSession.workspace_id == scope.workspace_id,
                ConversationSession.account_id == scope.account_id,
            )
            .order_by(ConversationSession.updated_at.desc())
            .limit(limit)
        )
    )


def latest_conversation(
    db: Session,
    *,
    scope: RequestScope | None = None,
) -> ConversationSession | None:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    return db.scalar(
        select(ConversationSession)
        .where(
            ConversationSession.workspace_id == scope.workspace_id,
            ConversationSession.account_id == scope.account_id,
        )
        .order_by(ConversationSession.updated_at.desc())
        .limit(1)
    )


def add_turn(
    db: Session,
    conversation: ConversationSession,
    role: str,
    content: str,
    *,
    scope: RequestScope | None = None,
    playbook_version_id: uuid.UUID | None,
    request_id: uuid.UUID | None = None,
    status: str = "complete",
    error_type: str | None = None,
) -> ConversationTurn:
    scope = _legacy_scope(scope)
    if status not in {"pending", "complete", "partial", "failed"}:
        raise ValueError("invalid conversation turn status")
    if error_type is not None and status not in {"partial", "failed"}:
        raise ValueError("error_type is only valid for partial or failed turns")
    validate_strategy_scope(db, scope, playbook_version_id)
    _validate_conversation_scope(conversation, scope)
    turn = ConversationTurn(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        session_id=conversation.id,
        playbook_version_id=playbook_version_id,
        role=role,
        content=content,
        request_id=request_id,
        status=status,
        error_type=error_type,
        created_at=datetime.now(UTC),
    )
    conversation.updated_at = datetime.now(UTC)
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return turn


def update_turn_outcome(
    db: Session,
    turn: ConversationTurn,
    *,
    scope: RequestScope | None = None,
    status: str,
    error_type: str | None = None,
) -> ConversationTurn:
    """Finalize one persisted request turn without rewriting its original content."""
    scope = _legacy_scope(scope)
    if status not in {"complete", "partial", "failed"}:
        raise ValueError("turn outcome must be complete, partial, or failed")
    if error_type is not None and status == "complete":
        raise ValueError("a completed turn cannot have an error type")
    if turn.workspace_id != scope.workspace_id or turn.account_id != scope.account_id:
        raise LookupError("conversation turn was not found in the requested account scope")
    turn.status = status
    turn.error_type = error_type
    db.commit()
    db.refresh(turn)
    return turn


def conversation_history(
    db: Session,
    conversation: ConversationSession,
    *,
    scope: RequestScope | None = None,
    playbook_version_id: uuid.UUID | None,
    limit: int = 20,
) -> list[dict[str, str]]:
    scope = _legacy_scope(scope)
    validate_strategy_scope(db, scope, playbook_version_id)
    _validate_conversation_scope(conversation, scope)
    strategy_scope = (
        ConversationTurn.playbook_version_id == playbook_version_id
        if playbook_version_id is not None
        else ConversationTurn.playbook_version_id.is_(None)
    )
    recent = list(
        db.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.workspace_id == scope.workspace_id,
                ConversationTurn.account_id == scope.account_id,
                ConversationTurn.session_id == conversation.id,
                strategy_scope,
                ConversationTurn.status == "complete",
            )
            .order_by(ConversationTurn.created_at.desc())
            .limit(limit)
        )
    )
    recent.reverse()
    return [{"role": turn.role, "content": turn.content} for turn in recent]


def conversation_transcript(
    db: Session,
    conversation: ConversationSession,
    *,
    scope: RequestScope | None = None,
    limit: int = 100,
) -> list[dict[str, str]]:
    """Return the complete audit transcript without using it as model context."""
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    _validate_conversation_scope(conversation, scope)
    recent = list(
        db.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.workspace_id == scope.workspace_id,
                ConversationTurn.account_id == scope.account_id,
                ConversationTurn.session_id == conversation.id,
            )
            .order_by(ConversationTurn.created_at.desc())
            .limit(limit)
        )
    )
    recent.reverse()
    transcript: list[dict[str, str]] = []
    for turn in recent:
        item = {"role": turn.role, "content": turn.content}
        if turn.status != "complete":
            item["status"] = turn.status
        if turn.error_type is not None:
            item["error_type"] = turn.error_type
        transcript.append(item)
    return transcript


def _validate_conversation_scope(
    conversation: ConversationSession,
    scope: RequestScope,
) -> None:
    if (
        conversation.workspace_id != scope.workspace_id
        or conversation.account_id != scope.account_id
    ):
        raise LookupError("conversation was not found in the requested account scope")
