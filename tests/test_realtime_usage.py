import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError

from app.services import realtime_usage
from app.services.workspaces import RequestScope


def _payload() -> dict:
    return {
        "response_id": "resp_123",
        "model": "gpt-realtime-2.1-mini",
        "input_text_tokens": 12,
        "input_audio_tokens": 30,
        "cached_text_tokens": 8,
        "cached_audio_tokens": 10,
        "output_text_tokens": 6,
        "output_audio_tokens": 20,
        "estimated_cost_usd": Decimal("0.001234"),
    }


def _duplicate_database(existing) -> Mock:
    database = Mock()
    database.scalar.side_effect = (SimpleNamespace(id=uuid.uuid4()), existing)
    database.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    return database


def test_realtime_usage_identical_retry_is_idempotent(monkeypatch) -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    session_id = uuid.uuid4()
    existing = SimpleNamespace(**_payload())
    database = _duplicate_database(existing)
    monkeypatch.setattr(realtime_usage, "validate_scope", lambda *_args: None)

    result = realtime_usage.record_realtime_usage(
        database,
        scope=scope,
        session_id=session_id,
        **_payload(),
    )

    assert result is existing
    database.rollback.assert_called_once_with()


def test_realtime_usage_rejects_conflicting_retry(monkeypatch) -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    session_id = uuid.uuid4()
    existing = SimpleNamespace(**{**_payload(), "output_audio_tokens": 21})
    database = _duplicate_database(existing)
    monkeypatch.setattr(realtime_usage, "validate_scope", lambda *_args: None)

    with pytest.raises(
        realtime_usage.RealtimeUsageConflictError,
        match="output_audio_tokens",
    ):
        realtime_usage.record_realtime_usage(
            database,
            scope=scope,
            session_id=session_id,
            **_payload(),
        )
