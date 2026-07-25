import re
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConversationSession, ConversationTurn

SESSION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_session_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized or len(normalized) > 80 or not SESSION_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "session name must contain letters or numbers and be at most 80 characters"
        )
    return normalized


def get_conversation_by_name(db: Session, name: str) -> ConversationSession | None:
    return db.scalar(
        select(ConversationSession).where(
            ConversationSession.name == normalize_session_name(name)
        )
    )


def resolve_conversation(db: Session, reference: str) -> ConversationSession | None:
    try:
        return get_conversation(db, uuid.UUID(reference))
    except ValueError:
        return get_conversation_by_name(db, reference)


def _available_daily_name(db: Session) -> str:
    base = f"daily-{date.today().isoformat()}"
    if get_conversation_by_name(db, base) is None:
        return base
    suffix = 2
    while get_conversation_by_name(db, f"{base}-{suffix}") is not None:
        suffix += 1
    return f"{base}-{suffix}"


def create_conversation(
    db: Session,
    name: str | None = None,
    title: str = "Trading Agent session",
) -> ConversationSession:
    session_name = normalize_session_name(name) if name else _available_daily_name(db)
    if get_conversation_by_name(db, session_name):
        raise ValueError(f"session name already exists: {session_name}")
    conversation = ConversationSession(name=session_name, title=title)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def get_conversation(db: Session, session_id: uuid.UUID) -> ConversationSession | None:
    return db.get(ConversationSession, session_id)


def list_conversations(db: Session, limit: int = 20) -> list[ConversationSession]:
    return list(
        db.scalars(
            select(ConversationSession)
            .order_by(ConversationSession.updated_at.desc())
            .limit(limit)
        )
    )


def latest_conversation(db: Session) -> ConversationSession | None:
    return db.scalar(
        select(ConversationSession).order_by(ConversationSession.updated_at.desc()).limit(1)
    )


def add_turn(
    db: Session,
    conversation: ConversationSession,
    role: str,
    content: str,
    *,
    playbook_version_id: uuid.UUID | None,
) -> ConversationTurn:
    turn = ConversationTurn(
        session_id=conversation.id,
        playbook_version_id=playbook_version_id,
        role=role,
        content=content,
    )
    conversation.updated_at = datetime.now(UTC)
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return turn


def conversation_history(
    db: Session,
    conversation: ConversationSession,
    *,
    playbook_version_id: uuid.UUID | None,
    limit: int = 20,
) -> list[dict[str, str]]:
    strategy_scope = (
        ConversationTurn.playbook_version_id == playbook_version_id
        if playbook_version_id is not None
        else ConversationTurn.playbook_version_id.is_(None)
    )
    recent = list(
        db.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.session_id == conversation.id,
                strategy_scope,
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
    limit: int = 100,
) -> list[dict[str, str]]:
    """Return the complete audit transcript without using it as model context."""
    recent = list(
        db.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.session_id == conversation.id)
            .order_by(ConversationTurn.created_at.desc())
            .limit(limit)
        )
    )
    recent.reverse()
    return [{"role": turn.role, "content": turn.content} for turn in recent]
