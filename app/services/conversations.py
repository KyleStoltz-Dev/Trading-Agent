import re
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConversationSession, ConversationTurn
from app.services.trading_workflow import is_dangling_count_clarification
from app.services.workspaces import (
    RequestScope,
    validate_scope,
    validate_strategy_scope,
)

SESSION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GENERIC_SESSION_TITLES = frozenset(
    {
        "dashboard trading desk",
        "pippy voice session",
        "trading agent session",
    }
)
TITLE_PREFIXES = (
    r"^hey[,\s]+",
    r"^(?:hey\s+)?(?:pippy|trading agent)[:,\s]+",
    r"^(?:could|can|would|will)\s+you\s+(?:please\s+)?",
    r"^(?:please\s+)?help\s+me\s+(?:to\s+)?",
    r"^(?:i\s+(?:would|'d)\s+like\s+to|i\s+want\s+(?:you\s+)?to|let'?s)\s+",
    r"^(?:please\s+)?(?:tell|show|give)\s+me\s+",
)
TITLE_STOP_WORDS = frozenset(
    {"a", "an", "and", "as", "at", "for", "in", "of", "on", "or", "the", "to", "with"}
)
TITLE_ACRONYMS = {
    "ai": "AI",
    "api": "API",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "ict": "ICT",
    "ollama": "Ollama",
    "oanda": "OANDA",
    "openai": "OpenAI",
    "pippy": "Pippy",
    "pnl": "PnL",
    "postgres": "Postgres",
    "wyckoff": "Wyckoff",
}
WEAK_TITLE_PREFIXES = (
    "continue our previous",
    "continue the previous",
    "from our last conversation",
    "from the last conversation",
    "pick up where we",
    "what were we discussing",
)


def is_generic_conversation_title(value: str) -> bool:
    return " ".join(value.split()).casefold() in GENERIC_SESSION_TITLES


def generate_conversation_title(message: str, *, fallback: str = "Pippy conversation") -> str:
    """Create a short local topic title without another model or external disclosure."""

    topic = re.sub(r"[`*_#]", "", " ".join(message.split())).strip(" \t\r\n.,!?;:-")
    for pattern in TITLE_PREFIXES:
        topic = re.sub(pattern, "", topic, count=1, flags=re.IGNORECASE).strip()
    topic = re.split(r"[.!?](?:\s|$)", topic, maxsplit=1)[0]
    topic = re.split(
        r"\b(?:that\s+(?:is|are|was|were)|so\s+from\s+what|because|but)\b",
        topic,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" \t\r\n.,!?;:-")
    words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", topic)[:8]
    if not words:
        return fallback
    formatted: list[str] = []
    for index, word in enumerate(words):
        lowered = word.casefold()
        if lowered in TITLE_ACRONYMS:
            formatted.append(TITLE_ACRONYMS[lowered])
        elif index and lowered in TITLE_STOP_WORDS:
            formatted.append(lowered)
        else:
            formatted.append(word[:1].upper() + word[1:].lower())
    title = " ".join(formatted).strip()
    return title[:160] or fallback


def is_weak_conversation_title(value: str) -> bool:
    normalized = " ".join(value.split()).casefold()
    return len(normalized.split()) < 3 or normalized.startswith(WEAK_TITLE_PREFIXES)


def conversation_topic_title(
    messages: list[str],
    *,
    fallback: str,
) -> str:
    generated = [
        generate_conversation_title(message, fallback=fallback)
        for message in messages
        if isinstance(message, str) and message.strip()
    ]
    if not generated:
        return "Empty Session"
    return next(
        (title for title in generated if not is_weak_conversation_title(title)),
        generated[0],
    )


def conversation_display_title(
    db: Session,
    conversation: ConversationSession,
    *,
    scope: RequestScope,
) -> str:
    """Return a useful title for legacy sessions without mutating during a read."""

    _validate_conversation_scope(conversation, scope)
    if not is_generic_conversation_title(conversation.title):
        return conversation.title
    opening_messages = db.scalars(
        select(ConversationTurn.content)
        .where(
            ConversationTurn.workspace_id == scope.workspace_id,
            ConversationTurn.account_id == scope.account_id,
            ConversationTurn.session_id == conversation.id,
            ConversationTurn.role == "user",
        )
        .order_by(ConversationTurn.created_at.asc())
        .limit(4)
    ).all()
    if not isinstance(opening_messages, list):
        return conversation.title
    return conversation_topic_title(opening_messages, fallback=conversation.title)


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
    scope: RequestScope,
) -> ConversationSession | None:
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
    scope: RequestScope,
) -> ConversationSession | None:
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
    scope: RequestScope,
) -> ConversationSession:
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
    scope: RequestScope,
) -> ConversationSession | None:
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
    scope: RequestScope,
) -> list[ConversationSession]:
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
    scope: RequestScope,
) -> ConversationSession | None:
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
    scope: RequestScope,
    playbook_version_id: uuid.UUID | None,
    request_id: uuid.UUID | None = None,
    status: str = "complete",
    error_type: str | None = None,
) -> ConversationTurn:
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
    if role == "user" and (
        is_generic_conversation_title(conversation.title)
        or is_weak_conversation_title(conversation.title)
    ):
        candidate = generate_conversation_title(content, fallback=conversation.title)
        if is_generic_conversation_title(conversation.title) or not is_weak_conversation_title(
            candidate
        ):
            conversation.title = candidate
    conversation.updated_at = datetime.now(UTC)
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return turn


def update_turn_outcome(
    db: Session,
    turn: ConversationTurn,
    *,
    scope: RequestScope,
    status: str,
    error_type: str | None = None,
) -> ConversationTurn:
    """Finalize one persisted request turn without rewriting its original content."""
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
    scope: RequestScope,
    playbook_version_id: uuid.UUID | None,
    limit: int = 20,
) -> list[dict[str, str]]:
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
    history = [{"role": turn.role, "content": turn.content} for turn in recent]
    reusable: list[dict[str, str]] = []
    index = 0
    while index < len(history):
        current = history[index]
        following = history[index + 1] if index + 1 < len(history) else None
        if (
            current["role"] == "user"
            and following is not None
            and following["role"] == "assistant"
            and is_dangling_count_clarification(
                current["content"],
                following["content"],
            )
        ):
            index += 2
            continue
        reusable.append(current)
        index += 1
    return reusable


def conversation_transcript(
    db: Session,
    conversation: ConversationSession,
    *,
    scope: RequestScope,
    limit: int = 100,
) -> list[dict[str, str]]:
    """Return the complete audit transcript without using it as model context."""
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
