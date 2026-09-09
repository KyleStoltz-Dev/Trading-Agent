"""Safe, copy-ready setup values for TradingView chart alerts."""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

_WEBHOOK_PATH = "/api/webhooks/tradingview/{account_id}"
_TRADINGVIEW_PATH_MARKER = "/api/webhooks/tradingview/"
_CONNECT_TERMS = re.compile(
    r"\b(connect|connection|link|set\s*up|setup|configure|enable)\b",
    re.IGNORECASE,
)


def _public_https_url(value: str) -> str:
    """Validate a user-supplied public HTTPS origin or webhook URL without fetching it."""
    candidate = value.strip().rstrip("/")
    if not candidate:
        raise ValueError("Enter the public HTTPS address for Trading Agent.")
    if len(candidate) > 2048:
        raise ValueError("The public address is too long.")
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() != "https":
        raise ValueError("TradingView needs a public HTTPS address.")
    if not parsed.hostname:
        raise ValueError("The public HTTPS address is missing a host name.")
    if parsed.username or parsed.password:
        raise ValueError("Do not put credentials in the public address.")
    if parsed.query or parsed.fragment:
        raise ValueError("Remove query parameters and fragments from the public address.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The public HTTPS address has an invalid port.") from exc
    if port not in {None, 443}:
        raise ValueError("Use the standard HTTPS port 443 for TradingView alerts.")

    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("TradingView cannot deliver alerts to a local-only address.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("TradingView cannot deliver alerts to a private network address.")

    clean_path = parsed.path.rstrip("/")
    return urlunsplit(("https", parsed.netloc, clean_path, "", ""))


def tradingview_webhook_url(public_url: str, account_id: uuid.UUID) -> str:
    """Return the exact account-scoped endpoint from an origin or complete endpoint."""
    normalized = _public_https_url(public_url)
    parsed = urlsplit(normalized)
    expected_path = _WEBHOOK_PATH.format(account_id=account_id)
    if _TRADINGVIEW_PATH_MARKER in parsed.path:
        if parsed.path != expected_path:
            raise ValueError(
                "That TradingView webhook address belongs to a different account or path."
            )
        return normalized
    path = f"{parsed.path}{expected_path}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def tradingview_alert_message(webhook_secret: str) -> str:
    """Build a valid TradingView JSON message with no broker or API credentials."""
    if len(webhook_secret) < 32:
        raise ValueError("The TradingView webhook secret is too short.")
    payload = {
        "webhook_secret": webhook_secret,
        "sent_at": "{{timenow}}",
        "event_id": "chart-{{exchange}}-{{ticker}}-{{interval}}-{{time}}",
        "alert_name": "Trading Agent chart alert",
        "exchange": "{{exchange}}",
        "symbol": "{{ticker}}",
        "timeframe": "{{interval}}",
        "event_type": "chart_alert",
        "condition": "Trader-defined TradingView alert condition fired",
        "market_time": "{{time}}",
        "open": "{{open}}",
        "high": "{{high}}",
        "low": "{{low}}",
        "close": "{{close}}",
        "volume": "{{volume}}",
        "metadata": {"source": "tradingview-chart"},
    }
    return json.dumps(payload, indent=2)


def is_tradingview_connection_request(message: str) -> bool:
    """Recognize a direct natural-language request to configure TradingView alerts."""
    normalized = " ".join(message.casefold().split())
    mentions_tradingview = "tradingview" in normalized or "trading view" in normalized
    return mentions_tradingview and _CONNECT_TERMS.search(normalized) is not None
