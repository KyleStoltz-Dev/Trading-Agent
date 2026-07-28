import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from app.config import Settings
from app.models import BrokerConnection, TradingAccount
from app.services import health as health_module


def test_enabled_tradingview_requires_secret_on_selected_account(
    monkeypatch,
    db_session,
    workspace_account,
    request_scope,
) -> None:
    _, account = workspace_account
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute = Mock()
    engine = Mock()
    engine.connect.return_value = connection
    monkeypatch.setattr(
        health_module,
        "inspect_schema",
        Mock(
            return_value=SimpleNamespace(
                legacy_unmanaged=False,
                current=True,
                current_revision="head",
                head_revision="head",
            )
        ),
    )
    monkeypatch.setattr(
        health_module,
        "Session",
        lambda _engine: nullcontext(db_session),
    )
    settings = Settings(
        _env_file=None,
        tradingview_webhook_enabled=True,
        broker_provider="none",
    )

    missing = health_module.check_health(settings, engine, scope=request_scope)
    check = next(
        item for item in missing.checks if item.name == "tradingview_account_secret"
    )
    assert check.status == "error"
    assert "trade account tradingview-secret" in check.detail

    account.tradingview_webhook_secret_sha256 = "a" * 64
    db_session.flush()
    configured = health_module.check_health(settings, engine, scope=request_scope)
    check = next(
        item for item in configured.checks
        if item.name == "tradingview_account_secret"
    )
    assert check.status == "ok"


def test_broker_health_ignores_connections_from_other_accounts(
    monkeypatch,
    db_session,
    workspace_account,
    request_scope,
) -> None:
    workspace, selected = workspace_account
    other = TradingAccount(
        workspace_id=workspace.id,
        broker="OANDA",
        external_account_id=f"other-{uuid.uuid4().hex}",
        label="Other",
        currency="USD",
        mode="practice",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(
        BrokerConnection(
            workspace_id=workspace.id,
            account_id=other.id,
            provider="oanda-v20",
            environment="practice",
            status="degraded",
        )
    )
    db_session.flush()
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute = Mock()
    engine = Mock()
    engine.connect.return_value = connection
    monkeypatch.setattr(
        health_module,
        "inspect_schema",
        Mock(
            return_value=SimpleNamespace(
                legacy_unmanaged=False,
                current=True,
                current_revision="head",
                head_revision="head",
            )
        ),
    )
    monkeypatch.setattr(
        health_module,
        "Session",
        lambda _engine: nullcontext(db_session),
    )

    report = health_module.check_health(
        Settings(_env_file=None, broker_provider="none"),
        engine,
        scope=request_scope,
    )

    assert not any(
        item.name.startswith("broker_connection:") for item in report.checks
    )
    assert selected.id == request_scope.account_id
