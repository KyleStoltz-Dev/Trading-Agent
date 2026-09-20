"""Resolve the read-only broker registered to the selected trading account."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import BrokerConfigurationError
from app.connectors.factory import validate_broker_account_selection
from app.models import BrokerConnection, TradingAccount
from app.services.workspaces import RequestScope

SUPPORTED_REGISTERED_BROKERS = frozenset(
    {
        "oanda-v20",
        "metatrader-mt4-bridge",
        "metatrader-mt5-bridge",
    }
)


def _requested_connection_provider(
    configured_provider: str,
    *,
    metatrader_platform: str,
) -> str | None:
    if configured_provider == "oanda":
        return "oanda-v20"
    if configured_provider == "metatrader":
        return f"metatrader-{metatrader_platform}-bridge"
    if configured_provider == "none":
        return None
    if configured_provider in {"ibkr", "alpaca", "twelve-data", "ctrader"}:
        raise BrokerConfigurationError(
            f"BROKER_PROVIDER={configured_provider} is planned and not available "
            "for live reads yet"
        )
    raise BrokerConfigurationError("the selected broker provider is unsupported")


def selected_account_broker_connection(
    db: Session,
    *,
    scope: RequestScope,
    configured_provider: str,
    metatrader_platform: str,
) -> tuple[TradingAccount, BrokerConnection]:
    """Return one supported connection, inferring it when the setting is unset.

    A registered account connection is durable application state. A missing optional
    environment preference must not hide that connection after Quickstart or a model-only
    settings change.
    """
    account = db.scalar(
        select(TradingAccount).where(
            TradingAccount.workspace_id == scope.workspace_id,
            TradingAccount.id == scope.account_id,
        )
    )
    if account is None:
        raise LookupError("selected trading account no longer exists")
    if not account.active:
        raise BrokerConfigurationError("the selected trading account is archived")

    requested = _requested_connection_provider(
        configured_provider,
        metatrader_platform=metatrader_platform,
    )
    statement = select(BrokerConnection).where(
        BrokerConnection.workspace_id == scope.workspace_id,
        BrokerConnection.account_id == scope.account_id,
        BrokerConnection.provider.in_(SUPPORTED_REGISTERED_BROKERS),
        BrokerConnection.status != "disabled",
    )
    if requested is not None:
        statement = statement.where(BrokerConnection.provider == requested)
    matches = list(db.scalars(statement))

    if requested is None and len(matches) > 1:
        account_provider = {
            "OANDA": "oanda-v20",
            "MT4": "metatrader-mt4-bridge",
            "MT5": "metatrader-mt5-bridge",
        }.get(account.broker.upper())
        if account_provider is not None:
            matches = [item for item in matches if item.provider == account_provider]

    if len(matches) == 1:
        validate_broker_account_selection(None, account, matches[0])
        return account, matches[0]
    if not matches:
        raise BrokerConfigurationError(
            "the selected account has no configured read-only broker connection"
        )
    raise BrokerConfigurationError(
        "the selected account has multiple broker connections; choose one in setup"
    )
