import uuid

import pytest
from sqlalchemy import select

from app.models import ToolExecutionAudit
from app.policy import PolicyViolation
from app.services.tool_audit import (
    AuditedToolExecutor,
    complete_mutation_audit,
    record_direct_cli_confirmation,
)

MUTATION_METADATA = {
    "mutate": {"mutating": True, "deterministic": True},
    "read": {"mutating": False, "deterministic": True},
}


def test_successful_mutation_is_durable_and_idempotent_per_request(
    db_session,
    request_scope,
) -> None:
    calls: list[dict] = []

    def execute(name: str, arguments: dict) -> str:
        calls.append(arguments)
        return '{"saved":true}'

    request_id = uuid.uuid4()
    audited = AuditedToolExecutor(
        db_session,
        execute,
        MUTATION_METADATA,
        scope=request_scope,
        request_id=request_id,
    )

    assert audited("mutate", {"value": 1}) == '{"saved":true}'
    assert audited("mutate", {"value": 1}) == '{"saved":true}'
    assert calls == [{"value": 1}]

    record = db_session.scalar(
        select(ToolExecutionAudit).where(
            ToolExecutionAudit.request_id == request_id
        )
    )
    assert record is not None
    assert record.status == "succeeded"
    assert record.arguments == {"value": 1}
    assert record.result_text == '{"saved":true}'
    assert len(record.result_hash or "") == 64


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (PolicyViolation("trader declined mutation"), "declined"),
        (RuntimeError("provider failed"), "failed"),
    ],
)
def test_mutation_failure_state_survives_rollback(
    db_session,
    request_scope,
    exception,
    expected_status,
) -> None:
    def execute(name: str, arguments: dict) -> str:
        raise exception

    request_id = uuid.uuid4()
    audited = AuditedToolExecutor(
        db_session,
        execute,
        MUTATION_METADATA,
        scope=request_scope,
        request_id=request_id,
    )

    with pytest.raises(type(exception), match=str(exception)):
        audited("mutate", {"value": 2})

    record = db_session.scalar(
        select(ToolExecutionAudit).where(
            ToolExecutionAudit.request_id == request_id
        )
    )
    assert record is not None
    assert record.status == expected_status
    assert record.failure_type == type(exception).__name__
    assert record.completed_at is not None


def test_read_only_tools_do_not_create_mutation_audits(
    db_session,
    request_scope,
) -> None:
    request_id = uuid.uuid4()
    audited = AuditedToolExecutor(
        db_session,
        lambda name, arguments: "read result",
        MUTATION_METADATA,
        scope=request_scope,
        request_id=request_id,
    )

    assert audited("read", {"value": 1}) == "read result"
    assert db_session.scalar(
        select(ToolExecutionAudit).where(
            ToolExecutionAudit.request_id == request_id
        )
    ) is None


def test_direct_cli_confirmation_is_durable_before_service_mutation(
    db_session,
    request_scope,
) -> None:
    record = record_direct_cli_confirmation(
        db_session,
        scope=request_scope,
        action="configure_broker_connection",
        arguments={"provider": "oanda-v20", "account": "Practice"},
    )

    assert record.status == "confirmed"
    assert record.confirmed_at is not None
    assert record.completed_at is None
    assert record.arguments == {
        "provider": "oanda-v20",
        "account": "Practice",
    }

    complete_mutation_audit(
        db_session,
        record.id,
        scope=request_scope,
    )
    db_session.refresh(record)
    assert record.status == "succeeded"
    assert record.completed_at is not None


def test_direct_cli_failure_is_durable(db_session, request_scope) -> None:
    record = record_direct_cli_confirmation(
        db_session,
        scope=request_scope,
        action="configure_broker_connection",
        arguments={"provider": "oanda-v20"},
    )

    complete_mutation_audit(
        db_session,
        record.id,
        scope=request_scope,
        error=RuntimeError("configuration failed"),
    )
    db_session.refresh(record)
    assert record.status == "failed"
    assert record.failure_type == "RuntimeError"
