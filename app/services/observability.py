"""Structured integration events with conservative recursive redaction."""

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

SENSITIVE_FIELD = re.compile(
    r"(?:authorization|cookie|password|secret|token|api[_-]?key|credential)",
    re.IGNORECASE,
)
SECRET_VALUE = re.compile(
    r"(?:Bearer\s+)?(?:sk-[A-Za-z0-9_-]{12,}|"
    r"gh[opusr]_[A-Za-z0-9]{16,}|"
    r"[A-Za-z0-9_-]{32,})"
)
REDACTED = "[REDACTED]"


def redact_event_value(value: Any, *, field: str | None = None) -> Any:
    """Return a JSON-compatible value without credential-shaped content."""
    if field is not None and SENSITIVE_FIELD.search(field):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(key): redact_event_value(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [redact_event_value(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub(REDACTED, value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)


def structured_event(
    name: str,
    *,
    component: str,
    outcome: str,
    fields: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", name):
        raise ValueError("event name must be a stable lowercase identifier")
    timestamp = occurred_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    return {
        "schema": "trading-agent.integration-event.v1",
        "occurred_at": timestamp.astimezone(UTC).isoformat(),
        "event": name,
        "component": component,
        "outcome": outcome,
        "fields": redact_event_value(fields or {}),
    }


def event_json(event: Mapping[str, Any]) -> str:
    return json.dumps(
        redact_event_value(event),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


class JsonEventSink:
    """Small dependency-free sink suitable for CLI, tests, or log collectors."""

    def __init__(self, writer: Callable[[str], Any]) -> None:
        self._writer = writer

    def emit(
        self,
        name: str,
        *,
        component: str,
        outcome: str,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = structured_event(
            name,
            component=component,
            outcome=outcome,
            fields=fields,
        )
        self._writer(event_json(event))
        return event
