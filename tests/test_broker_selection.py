import uuid

import pytest

from app.connectors import BrokerConfigurationError
from app.models import BrokerConnection, TradingAccount, Workspace
from app.services.broker_selection import selected_account_broker_connection
from app.services.workspaces import RequestScope


def _account(db_session, *, broker: str) -> tuple[TradingAccount, RequestScope]:
    workspace = Workspace(slug=f"broker-{uuid.uuid4().hex}", name="Broker selection")
    db_session.add(workspace)
    db_session.flush()
    account = TradingAccount(
        workspace_id=workspace.id,
        broker=broker,
        external_account_id=f"account-{uuid.uuid4().hex}",
        label="Selected account",
        currency="USD",
        mode="practice",
        is_default=True,
    )
    db_session.add(account)
    db_session.flush()
    return account, RequestScope(workspace.id, account.id)


def test_registered_oanda_connection_is_used_when_env_preference_is_none(
    db_session,
) -> None:
    account, scope = _account(db_session, broker="OANDA")
    connection = BrokerConnection(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        provider="oanda-v20",
        environment="practice",
        status="healthy",
    )
    db_session.add(connection)
    db_session.flush()

    selected_account, selected_connection = selected_account_broker_connection(
        db_session,
        scope=scope,
        configured_provider="none",
        metatrader_platform="mt5",
    )

    assert selected_account.id == account.id
    assert selected_connection.id == connection.id


def test_non_broker_import_connection_is_not_inferred(db_session) -> None:
    _, scope = _account(db_session, broker="TradingView")
    db_session.add(
        BrokerConnection(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            provider="tradingview-paper-import",
            environment="file-import",
            status="configured",
        )
    )
    db_session.flush()

    with pytest.raises(BrokerConfigurationError, match="no configured read-only broker"):
        selected_account_broker_connection(
            db_session,
            scope=scope,
            configured_provider="none",
            metatrader_platform="mt5",
        )


def test_disabled_registered_connection_is_not_inferred(db_session) -> None:
    _, scope = _account(db_session, broker="OANDA")
    db_session.add(
        BrokerConnection(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            provider="oanda-v20",
            environment="practice",
            status="disabled",
        )
    )
    db_session.flush()

    with pytest.raises(BrokerConfigurationError, match="no configured read-only broker"):
        selected_account_broker_connection(
            db_session,
            scope=scope,
            configured_provider="none",
            metatrader_platform="mt5",
        )


def test_resolver_never_falls_back_to_another_accounts_connection(db_session) -> None:
    _, selected_scope = _account(db_session, broker="OANDA")
    _, other_scope = _account(db_session, broker="OANDA")
    db_session.add(BrokerConnection(
        workspace_id=other_scope.workspace_id, account_id=other_scope.account_id,
        provider="oanda-v20", status="healthy", environment="practice",
    ))
    db_session.flush()
    with pytest.raises(BrokerConfigurationError, match="no configured"):
        selected_account_broker_connection(
            db_session, scope=selected_scope, configured_provider="none",
            metatrader_platform="mt5",
        )


@pytest.mark.parametrize("invalid_state", ["archived", "mismatched_broker"])
def test_resolver_rejects_invalid_durable_account_state(db_session, invalid_state) -> None:
    account, scope = _account(db_session, broker="OANDA")
    db_session.add(BrokerConnection(
        workspace_id=scope.workspace_id, account_id=scope.account_id,
        provider="oanda-v20", status="healthy", environment="practice",
    ))
    if invalid_state == "archived":
        account.active = False
    else:
        account.broker = "MT5"
    db_session.flush()
    with pytest.raises(BrokerConfigurationError):
        selected_account_broker_connection(
            db_session, scope=scope, configured_provider="none", metatrader_platform="mt5",
        )
