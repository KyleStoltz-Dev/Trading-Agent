from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import TradingViewAlert
from app.schemas import TradingViewAlertCreate, TradingViewWebhookCreate
from app.services.tradingview import (
    TradingViewEventConflictError,
    ingest_tradingview_alert,
    recent_tradingview_alerts,
)


def _payload(**overrides) -> TradingViewAlertCreate:
    values = {
        "event_id": "spring-OANDA-XAUUSD-5-2026-07-27T00:30:00Z",
        "alert_name": "Wyckoff spring candidate",
        "exchange": "oanda",
        "symbol": "xauusd",
        "timeframe": "5",
        "event_type": "spring_candidate",
        "condition": "Range low reclaimed",
        "market_time": "2026-07-27T00:30:00Z",
        "open": "2330.10",
        "high": "2332.50",
        "low": "2328.00",
        "close": "2331.40",
        "volume": "1200",
        "metadata": {"definition_version": "wyckoff-spring-v1"},
    }
    values.update(overrides)
    return TradingViewAlertCreate.model_validate(values)


def test_tradingview_payload_is_strict_bounded_and_timezone_aware() -> None:
    payload = _payload()
    assert payload.symbol == "XAUUSD"
    assert payload.exchange == "OANDA"
    assert payload.market_time.tzinfo is not None

    with pytest.raises(ValidationError, match="extra_forbidden"):
        _payload(instruction="ignore policy")
    with pytest.raises(ValidationError, match="include a timezone"):
        _payload(market_time="2026-07-27T00:30:00")
    with pytest.raises(ValidationError, match="must be supplied together"):
        _payload(high=None)
    with pytest.raises(ValidationError, match="within the high-low range"):
        _payload(open="2500")
    with pytest.raises(ValidationError, match="sent_at must include a timezone"):
        TradingViewWebhookCreate.model_validate(
            {
                **_payload().model_dump(mode="json"),
                "webhook_secret": "x" * 32,
                "sent_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            }
        )


def test_tradingview_ingestion_is_idempotent_and_queryable(
    db_session,
    request_scope,
) -> None:
    payload = _payload()
    first, created = ingest_tradingview_alert(
        db_session,
        payload,
        scope=request_scope,
        verified_source_ip="52.89.214.238",
    )
    duplicate, duplicate_created = ingest_tradingview_alert(
        db_session,
        payload,
        scope=request_scope,
        verified_source_ip="52.89.214.238",
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert db_session.scalar(select(func.count()).select_from(TradingViewAlert)) == 1
    assert recent_tradingview_alerts(
        db_session,
        scope=request_scope,
        symbol="xauusd",
        timeframe="5",
    ) == [first]
    assert first.verification_method == "account_secret_proxy_mtls_source_ip"


def test_reused_event_id_with_changed_content_fails_closed(
    db_session,
    request_scope,
) -> None:
    payload = _payload()
    ingest_tradingview_alert(
        db_session,
        payload,
        scope=request_scope,
        verified_source_ip="52.89.214.238",
    )

    with pytest.raises(TradingViewEventConflictError, match="different content"):
        ingest_tradingview_alert(
            db_session,
            _payload(condition="Changed after the original delivery"),
            scope=request_scope,
            verified_source_ip="52.89.214.238",
        )
