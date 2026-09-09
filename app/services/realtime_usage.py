"""Durable Pippy Realtime usage accounting."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ConversationSession, RealtimeUsageEvent
from app.services.workspaces import RequestScope, validate_scope


class RealtimeUsageConflictError(RuntimeError):
    """The same provider response ID was replayed with different accounting."""


_USAGE_FIELDS = (
    "model",
    "input_text_tokens",
    "input_audio_tokens",
    "cached_text_tokens",
    "cached_audio_tokens",
    "output_text_tokens",
    "output_audio_tokens",
    "estimated_cost_usd",
)


def record_realtime_usage(
    db: Session,
    *,
    scope: RequestScope,
    session_id,
    response_id: str,
    model: str,
    input_text_tokens: int,
    input_audio_tokens: int,
    cached_text_tokens: int,
    cached_audio_tokens: int,
    output_text_tokens: int,
    output_audio_tokens: int,
    estimated_cost_usd: Decimal,
) -> RealtimeUsageEvent:
    validate_scope(db, scope)
    conversation = db.scalar(
        select(ConversationSession).where(
            ConversationSession.workspace_id == scope.workspace_id,
            ConversationSession.account_id == scope.account_id,
            ConversationSession.id == session_id,
        )
    )
    if conversation is None:
        raise LookupError("conversation session was not found")
    event = RealtimeUsageEvent(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        session_id=session_id,
        response_id=response_id,
        model=model,
        input_text_tokens=input_text_tokens,
        input_audio_tokens=input_audio_tokens,
        cached_text_tokens=cached_text_tokens,
        cached_audio_tokens=cached_audio_tokens,
        output_text_tokens=output_text_tokens,
        output_audio_tokens=output_audio_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(RealtimeUsageEvent).where(
                RealtimeUsageEvent.workspace_id == scope.workspace_id,
                RealtimeUsageEvent.account_id == scope.account_id,
                RealtimeUsageEvent.session_id == session_id,
                RealtimeUsageEvent.response_id == response_id,
            )
        )
        if existing is None:
            raise
        conflicts = [
            field_name
            for field_name in _USAGE_FIELDS
            if getattr(existing, field_name) != getattr(event, field_name)
        ]
        if conflicts:
            raise RealtimeUsageConflictError(
                "Realtime usage response ID was already recorded with different "
                f"accounting fields: {', '.join(conflicts)}"
            ) from exc
        return existing
    db.refresh(event)
    return event
