"""Verified, replay-safe TradingView alert ingestion and retrieval."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
from datetime import UTC, datetime

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TradingAccount, TradingViewAlert
from app.schemas import TradingViewAlertCreate
from app.services.workspaces import RequestScope, validate_scope


class TradingViewEventConflictError(ValueError):
    """An event identifier was replayed with different content."""


def generate_tradingview_webhook_secret() -> tuple[str, str]:
    """Return a one-time plaintext secret and the only value stored in PostgreSQL."""
    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode("utf-8")).hexdigest()


def set_tradingview_webhook_secret(
    db: Session,
    *,
    account: TradingAccount,
) -> str:
    secret, digest = generate_tradingview_webhook_secret()
    account.tradingview_webhook_secret_sha256 = digest
    db.commit()
    return secret


def tradingview_webhook_secret_is_valid(
    account: TradingAccount,
    candidate: str,
) -> bool:
    expected = account.tradingview_webhook_secret_sha256
    if expected is None or len(candidate) < 32:
        return False
    supplied = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, supplied)


def trusted_proxy_networks(
    configured_cidrs: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = tuple(
        ipaddress.ip_network(value.strip(), strict=False)
        for value in configured_cidrs.split(",")
        if value.strip()
    )
    if not networks:
        raise ValueError("at least one trusted proxy CIDR is required")
    if any(
        (network.version == 4 and network.prefixlen < 24)
        or (network.version == 6 and network.prefixlen < 64)
        for network in networks
    ):
        raise ValueError("trusted proxy CIDRs are too broad")
    return networks


def _payload_digest(payload: TradingViewAlertCreate) -> str:
    canonical = json.dumps(
        jsonable_encoder(payload.model_dump(mode="json")),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ingest_tradingview_alert(
    db: Session,
    payload: TradingViewAlertCreate,
    *,
    scope: RequestScope,
    verified_source_ip: str,
) -> tuple[TradingViewAlert, bool]:
    """Persist one verified delivery and return (record, created)."""
    validate_scope(db, scope)
    digest = _payload_digest(payload)
    event_match = db.scalar(
        select(TradingViewAlert).where(
            TradingViewAlert.workspace_id == scope.workspace_id,
            TradingViewAlert.account_id == scope.account_id,
            TradingViewAlert.external_event_id == payload.event_id
        )
    )
    if event_match is not None:
        if event_match.payload_sha256 != digest:
            raise TradingViewEventConflictError(
                "TradingView event_id was already used with different content"
            )
        return event_match, False
    payload_match = db.scalar(
        select(TradingViewAlert).where(
            TradingViewAlert.workspace_id == scope.workspace_id,
            TradingViewAlert.account_id == scope.account_id,
            TradingViewAlert.payload_sha256 == digest,
        )
    )
    if payload_match is not None:
        return payload_match, False

    alert = TradingViewAlert(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        external_event_id=payload.event_id,
        alert_name=payload.alert_name,
        symbol=payload.symbol,
        exchange=payload.exchange,
        timeframe=payload.timeframe,
        event_type=payload.event_type,
        condition=payload.condition,
        market_time=payload.market_time.astimezone(UTC),
        received_at=datetime.now(UTC),
        open_price=payload.open,
        high_price=payload.high,
        low_price=payload.low,
        close_price=payload.close,
        volume=payload.volume,
        note=payload.note,
        metadata_json=payload.metadata,
        payload_sha256=digest,
        verified_source_ip=verified_source_ip,
        verification_method="account_secret_proxy_mtls_source_ip",
    )
    try:
        with db.begin_nested():
            db.add(alert)
            db.flush()
    except IntegrityError as exc:
        existing = db.scalar(
            select(TradingViewAlert).where(
                TradingViewAlert.workspace_id == scope.workspace_id,
                TradingViewAlert.account_id == scope.account_id,
                (
                    (TradingViewAlert.external_event_id == payload.event_id)
                    | (TradingViewAlert.payload_sha256 == digest)
                ),
            )
        )
        if existing is None:
            raise
        if existing.payload_sha256 != digest:
            raise TradingViewEventConflictError(
                "TradingView event_id was already used with different content"
            ) from exc
        return existing, False
    db.commit()
    db.refresh(alert)
    return alert, True


def recent_tradingview_alerts(
    db: Session,
    *,
    scope: RequestScope,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 20,
) -> list[TradingViewAlert]:
    validate_scope(db, scope)
    statement = select(TradingViewAlert).where(
        TradingViewAlert.workspace_id == scope.workspace_id,
        TradingViewAlert.account_id == scope.account_id,
    )
    if symbol:
        statement = statement.where(TradingViewAlert.symbol == symbol.upper())
    if timeframe:
        statement = statement.where(TradingViewAlert.timeframe == timeframe.upper())
    return list(
        db.scalars(
            statement.order_by(
                TradingViewAlert.market_time.desc(),
                TradingViewAlert.received_at.desc(),
            ).limit(limit)
        )
    )
