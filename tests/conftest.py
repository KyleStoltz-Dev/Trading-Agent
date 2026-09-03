import uuid

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import engine, upgrade_database
from app.models import TradingAccount, Workspace
from app.services.workspaces import RequestScope


@pytest.fixture
def db_session():
    try:
        upgrade_database()
        connection = engine.connect()
    except OperationalError:
        pytest.skip("PostgreSQL is unavailable")
    transaction = connection.begin()
    session = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    try:
        yield session
    finally:
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
