"""Consistent interrupted-request accounting for every conversational interface."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models import ConversationSession, ConversationTurn
from app.services.conversations import add_turn, update_turn_outcome
from app.services.workspaces import RequestScope

if TYPE_CHECKING:
    from app.services.agent import TradingAgent


def record_request_failure(
    db: Session,
    *,
    agent: TradingAgent,
    user_turn: ConversationTurn,
    conversation: ConversationSession,
    scope: RequestScope,
    playbook_version_id: uuid.UUID | None,
    request_id: uuid.UUID,
    error_type: str,
) -> bool:
    """Keep committed mutations, roll back unfinished work, and retain resumable intent."""
    partial = bool(agent.last_tool_audit and agent.last_tool_audit.succeeded)
    outcome = "partial" if partial else "failed"
    cancelled = error_type == "UserCancelled"
    db.rollback()
    update_turn_outcome(
        db, user_turn, scope=scope, status=outcome, error_type=error_type,
    )
    add_turn(
        db, conversation, "assistant",
        (
            "The request stopped after at least one confirmed database change. "
            "The completed tool audit was retained."
            if partial else (
                "The response was cancelled by the user."
                if cancelled else "The request failed before a complete response was produced."
            )
        ),
        scope=scope, playbook_version_id=playbook_version_id, request_id=request_id,
        status=outcome, error_type=error_type,
    )
    return partial
