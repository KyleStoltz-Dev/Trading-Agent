"""Read-only chat webhook normalization and idempotent message ingestion."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ChatWebhookMessage, TradingAccount
from app.services.workspaces import RequestScope, validate_scope


class ChatWebhookValidationError(ValueError):
    """Payload or platform cannot be ingested."""


class ChatWebhookReplayError(ValueError):
    """A message identifier conflicts with a different message payload."""


CHAT_PLATFORM_OPTIONS = frozenset({"telegram", "discord"})


def _normalize_platform(platform: str) -> str:
    normalized = platform.strip().lower()
    if normalized not in CHAT_PLATFORM_OPTIONS:
        raise ValueError(f"unsupported chat platform: {platform}")
    return normalized


def generate_chat_webhook_secret() -> tuple[str, str]:
    """Return plaintext secret plus digest to persist in PostgreSQL."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def set_chat_webhook_secret(
    db: Session,
    *,
    account: TradingAccount,
    platform: str,
) -> str:
    """Create or rotate a chat webhook secret for the selected account."""
    platform_key = _normalize_platform(platform)
    secret, digest = generate_chat_webhook_secret()
    if platform_key == "telegram":
        account.telegram_webhook_secret_sha256 = digest
    else:
        account.discord_webhook_secret_sha256 = digest
    db.commit()
    return secret


def chat_webhook_secret_is_valid(
    account: TradingAccount,
    platform: str,
    candidate: str,
) -> bool:
    """Validate candidate against stored hashed secret."""
    platform_key = _normalize_platform(platform)
    expected = (
        account.telegram_webhook_secret_sha256
        if platform_key == "telegram"
        else account.discord_webhook_secret_sha256
    )
    if expected is None or len(candidate) < 32:
        return False
    return hmac.compare_digest(
        expected,
        hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
    )


def _payload_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        jsonable_encoder(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


def _telegram_sender_name(sender: Mapping[str, Any] | None) -> str | None:
    if not sender:
        return None
    username = sender.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    first_name = sender.get("first_name")
    last_name = sender.get("last_name")
    if isinstance(first_name, str) or isinstance(last_name, str):
        return " ".join(
            part.strip() for part in (first_name or "", last_name or "") if part
        )
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_telegram_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    message: Mapping[str, Any] | None = None
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            message = candidate
            break
    if message is None:
        message = payload
    message_id = message.get("message_id")
    if not message_id:
        raise ChatWebhookValidationError("telegram payload does not include message_id")
    sender = message.get("from") if isinstance(message.get("from"), Mapping) else None
    chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else None

    text = message.get("text")
    if text is None:
        text = message.get("caption")
        if text is None:
            text = ""
    return {
        "external_message_id": str(message_id),
        "sender_id": _coerce_str(sender.get("id") if isinstance(sender, Mapping) else None)
        or "unknown",
        "sender_name": _telegram_sender_name(sender if isinstance(sender, Mapping) else None),
        "channel_id": _coerce_str(chat.get("id") if isinstance(chat, Mapping) else None),
        "channel_name": _coerce_str(
            chat.get("title")
            if isinstance(chat, Mapping) and chat.get("title")
            else chat.get("username") if isinstance(chat, Mapping) else None
        ),
        "sent_at": _parse_datetime(message.get("date")),
        "text": str(text),
    }


def _normalize_discord_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    message_id = payload.get("id")
    if not message_id:
        raise ChatWebhookValidationError("discord payload does not include id")
    author = payload.get("author") if isinstance(payload.get("author"), Mapping) else None
    return {
        "external_message_id": str(message_id),
        "sender_id": _coerce_str(author.get("id") if isinstance(author, Mapping) else None)
        or "unknown",
        "sender_name": _coerce_str(
            author.get("global_name")
            if isinstance(author, Mapping)
            else author.get("username") if isinstance(author, Mapping) else None
        ) or _coerce_str(author.get("username") if isinstance(author, Mapping) else None),
        "channel_id": _coerce_str(payload.get("channel_id")),
        "channel_name": None,
        "sent_at": _parse_datetime(payload.get("timestamp")),
        "text": str(payload.get("content", "")),
    }


def _normalize_payload(platform: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if platform == "telegram":
        return _normalize_telegram_payload(payload)
    if platform == "discord":
        return _normalize_discord_payload(payload)
    raise ValueError(f"unsupported chat platform: {platform}")


def ingest_chat_webhook_message(
    db: Session,
    *,
    payload: Mapping[str, Any],
    platform: str,
    scope: RequestScope,
) -> tuple[ChatWebhookMessage, bool]:
    """Persist a chat payload and deduplicate by message ID and payload hash."""
    validate_scope(db, scope)
    platform_key = _normalize_platform(platform)

    metadata = _normalize_payload(platform_key, payload)
    digest = _payload_digest(payload)

    external_message_id = metadata["external_message_id"]

    existing = db.scalar(
        select(ChatWebhookMessage).where(
            ChatWebhookMessage.workspace_id == scope.workspace_id,
            ChatWebhookMessage.account_id == scope.account_id,
            ChatWebhookMessage.platform == platform_key,
            ChatWebhookMessage.external_message_id == external_message_id,
        )
    )
    if existing is not None:
        if existing.payload_sha256 != digest:
            raise ChatWebhookReplayError(
                "chat message id was already used with different content"
            )
        return existing, False

    existing_payload = db.scalar(
        select(ChatWebhookMessage).where(
            ChatWebhookMessage.workspace_id == scope.workspace_id,
            ChatWebhookMessage.account_id == scope.account_id,
            ChatWebhookMessage.platform == platform_key,
            ChatWebhookMessage.payload_sha256 == digest,
        )
    )
    if existing_payload is not None:
        return existing_payload, False

    record = ChatWebhookMessage(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        platform=platform_key,
        external_message_id=external_message_id,
        sender_id=metadata["sender_id"],
        sender_name=metadata["sender_name"],
        channel_id=metadata["channel_id"],
        channel_name=metadata["channel_name"],
        sent_at=metadata["sent_at"],
        text=metadata["text"],
        verified_source=platform_key,
        metadata_json=jsonable_encoder(payload),
        payload_sha256=digest,
    )

    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        current = db.scalar(
            select(ChatWebhookMessage).where(
                ChatWebhookMessage.workspace_id == scope.workspace_id,
                ChatWebhookMessage.account_id == scope.account_id,
                ChatWebhookMessage.platform == platform_key,
                or_(
                    ChatWebhookMessage.external_message_id == external_message_id,
                    ChatWebhookMessage.payload_sha256 == digest,
                ),
            )
        )
        if current is None:
            raise
        if current.payload_sha256 != digest:
            raise ChatWebhookReplayError(
                "chat message id was already used with different content"
            ) from exc
        return current, False

    db.commit()
    db.refresh(record)
    return record, True


def recent_chat_webhooks(
    db: Session,
    *,
    scope: RequestScope,
    platform: str | None = None,
    limit: int = 20,
) -> list[ChatWebhookMessage]:
    """List inbound messages for the selected account and optional platform."""
    validate_scope(db, scope)
    statement = select(ChatWebhookMessage).where(
        ChatWebhookMessage.workspace_id == scope.workspace_id,
        ChatWebhookMessage.account_id == scope.account_id,
    )
    if platform is not None:
        platform_key = _normalize_platform(platform)
        statement = statement.where(ChatWebhookMessage.platform == platform_key)

    return list(
        db.scalars(
            statement.order_by(
                ChatWebhookMessage.received_at.desc(),
                ChatWebhookMessage.id.desc(),
            ).limit(limit)
        )
    )
