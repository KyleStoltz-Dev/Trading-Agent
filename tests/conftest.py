import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import engine, upgrade_database
from app.models import TradingAccount, Workspace
from app.services.workspaces import RequestScope

LEGACY_WORKSPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
LEGACY_UNASSIGNED_ACCOUNT_ID = uuid.UUID(
    "00000000-0000-4000-8000-000000000002"
)


def _apply_legacy_scope(session, _flush_context, _instances) -> None:
    """Keep pre-scope regression tests valid until their services are migrated."""
    for instance in session.new:
        table = getattr(instance, "__table__", None)
        if table is None:
            continue
        workspace_column = table.c.get("workspace_id")
        if (
            workspace_column is not None
            and not workspace_column.nullable
            and getattr(instance, "workspace_id", None) is None
        ):
            instance.workspace_id = LEGACY_WORKSPACE_ID
        account_column = table.c.get("account_id")
        if (
            account_column is not None
            and not account_column.nullable
            and getattr(instance, "account_id", None) is None
        ):
            instance.account_id = LEGACY_UNASSIGNED_ACCOUNT_ID


@pytest.fixture
def db_session():
    try:
        upgrade_database()
        connection = engine.connect()
    except OperationalError:
        pytest.skip("PostgreSQL is unavailable")
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    event.listen(session, "before_flush", _apply_legacy_scope)
    try:
        yield session
    finally:
        event.remove(session, "before_flush", _apply_legacy_scope)
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def workspace_account(db_session):
    """Create one real tenant/account pair for explicitly scoped service tests."""
    workspace = Workspace(
        slug=f"test-{uuid.uuid4().hex}",
        name="Test workspace",
    )
    db_session.add(workspace)
    db_session.flush()
    account = TradingAccount(
        workspace_id=workspace.id,
        broker="manual",
        external_account_id=f"test-{uuid.uuid4().hex}",
        label="Test account",
        currency="USD",
        mode="practice",
        is_default=True,
    )
    db_session.add(account)
    db_session.commit()
    return workspace, account


@pytest.fixture
def request_scope(workspace_account):
    workspace, account = workspace_account
    return RequestScope(workspace_id=workspace.id, account_id=account.id)
