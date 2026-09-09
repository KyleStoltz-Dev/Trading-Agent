import json
import uuid

import pytest

from app.schemas import TradingViewWebhookCreate
from app.services.tradingview_setup import (
    is_tradingview_connection_request,
    tradingview_alert_message,
    tradingview_webhook_url,
)

ACCOUNT_ID = uuid.UUID("00000000-0000-4000-8000-000000000123")


def test_tradingview_webhook_url_accepts_origin_or_exact_account_endpoint() -> None:
    expected = (
        "https://alerts.example.com/api/webhooks/tradingview/"
        "00000000-0000-4000-8000-000000000123"
    )

    assert tradingview_webhook_url("https://alerts.example.com/", ACCOUNT_ID) == expected
    assert tradingview_webhook_url(expected, ACCOUNT_ID) == expected


@pytest.mark.parametrize(
    "value",
    (
        "http://alerts.example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.8",
        "https://alerts.example.com:8443",
        "https://user:secret@alerts.example.com",
        "https://alerts.example.com?token=secret",
    ),
)
def test_tradingview_webhook_url_rejects_unreachable_or_sensitive_addresses(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        tradingview_webhook_url(value, ACCOUNT_ID)


def test_tradingview_webhook_url_rejects_a_different_account_endpoint() -> None:
    with pytest.raises(ValueError, match="different account"):
        tradingview_webhook_url(
            "https://alerts.example.com/api/webhooks/tradingview/"
            "00000000-0000-4000-8000-000000000999",
            ACCOUNT_ID,
        )


def test_tradingview_alert_message_is_valid_copy_ready_json() -> None:
    secret = "s" * 43

    payload = json.loads(tradingview_alert_message(secret))

    assert payload["webhook_secret"] == secret
    assert payload["sent_at"] == "{{timenow}}"
    assert "{{ticker}}" in payload["event_id"]
    assert payload["market_time"] == "{{time}}"
    assert payload["close"] == "{{close}}"
    assert "api" not in payload

    delivered = (
        tradingview_alert_message(secret)
        .replace("{{timenow}}", "2026-09-08T15:30:01Z")
        .replace("{{exchange}}", "OANDA")
        .replace("{{ticker}}", "XAUUSD")
        .replace("{{interval}}", "240")
        .replace("{{time}}", "2026-09-08T12:00:00Z")
        .replace("{{open}}", "2500.1")
        .replace("{{high}}", "2510.2")
        .replace("{{low}}", "2495.4")
        .replace("{{close}}", "2508.3")
        .replace("{{volume}}", "1200")
    )
    assert TradingViewWebhookCreate.model_validate_json(delivered).symbol == "XAUUSD"


@pytest.mark.parametrize(
    "message",
    (
        "Connect TradingView",
        "Can we link my Trading View demo account?",
        "I want to set up TradingView alerts",
    ),
)
def test_tradingview_connection_intent_is_recognized(message: str) -> None:
    assert is_tradingview_connection_request(message) is True


@pytest.mark.parametrize(
    "message",
    (
        "Show my latest TradingView alert",
        "What does TradingView store?",
        "Review this chart",
    ),
)
def test_read_only_tradingview_questions_do_not_launch_setup(message: str) -> None:
    assert is_tradingview_connection_request(message) is False
