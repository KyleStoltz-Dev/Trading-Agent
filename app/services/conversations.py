import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConversationSession, ConversationTurn


def create_conversation(db: Session, title: str = "Trading Agent session") -> ConversationSession:
    conversation = ConversationSession(title=title)
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
) -> ConversationTurn:
    turn = ConversationTurn(session_id=conversation.id, role=role, content=content)
    conversation.updated_at = datetime.now(UTC)
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return turn


def conversation_history(
    db: Session,
    conversation: ConversationSession,
    limit: int = 20,
) -> list[dict[str, str]]:
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
