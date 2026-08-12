from typing import TYPE_CHECKING

from app.config import Settings, secret_value
from app.connectors.alpaca import AlpacaReadOnlyConnector
from app.connectors.forex_factory import ForexFactoryReadOnlyConnector
from app.connectors.kraken import KrakenReadOnlyConnector
from app.connectors.metatrader_bridge import MetaTraderReadOnlyBridgeConnector
from app.connectors.oanda import OandaReadOnlyConnector
from app.connectors.trading_economics import TradingEconomicsReadOnlyConnector
from app.services.secrets import SecretBackendError, resolve_broker_credentials

if TYPE_CHECKING:
    from app.models import BrokerConnection, TradingAccount


class BrokerConfigurationError(ValueError):
    pass



_PLANNED_BROKER_PROVIDERS = frozenset({"ibkr", "alpaca", "twelve-data", "ctrader"})


def create_oanda_connector(settings: Settings) -> OandaReadOnlyConnector:
    if not settings.oanda_api_token or not settings.oanda_account_id:
        raise BrokerConfigurationError(
            "OANDA_API_TOKEN and OANDA_ACCOUNT_ID are required for OANDA reads"
        )
    return OandaReadOnlyConnector(
        token=secret_value(settings.oanda_api_token),
        account_id=secret_value(settings.oanda_account_id),
        environment=settings.oanda_environment,
        timeout_seconds=settings.oanda_request_timeout_seconds,
    )


def create_metatrader_connector(
    settings: Settings,
) -> MetaTraderReadOnlyBridgeConnector:
    token = secret_value(settings.metatrader_bridge_token)
    account_id = secret_value(settings.metatrader_account_id)
    if not token or not account_id:
        raise BrokerConfigurationError(
            "METATRADER_BRIDGE_TOKEN and METATRADER_ACCOUNT_ID are required "
            "for MetaTrader reads"
        )
    if len(token) < 32:
        raise BrokerConfigurationError(
            "METATRADER_BRIDGE_TOKEN must contain at least 32 characters"
        )
    try:
        return MetaTraderReadOnlyBridgeConnector(
            base_url=settings.metatrader_bridge_url,
            token=token,
            account_id=account_id,
            platform=settings.metatrader_platform,
            timeout_seconds=settings.metatrader_request_timeout_seconds,
            poll_interval_seconds=settings.metatrader_poll_interval_seconds,
            maximum_response_bytes=settings.metatrader_max_response_bytes,
            allow_insecure_remote=settings.metatrader_allow_insecure_remote,
        )
    except ValueError as exc:
        raise BrokerConfigurationError(str(exc)) from exc


def create_broker_connector(
    settings: Settings,
    *,
    account: "TradingAccount | None" = None,
    connection: "BrokerConnection | None" = None,
) -> OandaReadOnlyConnector | MetaTraderReadOnlyBridgeConnector:
    if account is not None:
        validate_broker_account_selection(settings, account, connection)
        if connection is None:
            raise BrokerConfigurationError(
                "the selected account has no registered broker connection"
            )
        try:
            credentials = resolve_broker_credentials(
                settings,
                provider=connection.provider,
                reference=connection.config_reference,
            )
        except SecretBackendError as exc:
            raise BrokerConfigurationError(str(exc)) from exc
        if connection.provider == "oanda-v20":
            return OandaReadOnlyConnector(
                token=credentials.token,
                account_id=account.external_account_id,
                environment=connection.environment,
                timeout_seconds=settings.oanda_request_timeout_seconds,
            )
        if connection.provider in {
            "metatrader-mt4-bridge",
            "metatrader-mt5-bridge",
        }:
            platform = "mt4" if "mt4" in connection.provider else "mt5"
            try:
                return MetaTraderReadOnlyBridgeConnector(
                    base_url=settings.metatrader_bridge_url,
                    token=credentials.token,
                    account_id=account.external_account_id,
                    platform=platform,
                    timeout_seconds=settings.metatrader_request_timeout_seconds,
                    poll_interval_seconds=settings.metatrader_poll_interval_seconds,
                    maximum_response_bytes=settings.metatrader_max_response_bytes,
                    allow_insecure_remote=settings.metatrader_allow_insecure_remote,
                )
            except ValueError as exc:
                raise BrokerConfigurationError(str(exc)) from exc
    if settings.broker_provider in _PLANNED_BROKER_PROVIDERS:
        raise BrokerConfigurationError(
            f"BROKER_PROVIDER={settings.broker_provider} is planned and not implemented yet"
        )
    if settings.broker_provider == "oanda":
        return create_oanda_connector(settings)
    if settings.broker_provider == "metatrader":
        return create_metatrader_connector(settings)
    raise BrokerConfigurationError(
        "BROKER_PROVIDER must be oanda or metatrader for broker reads"
    )


def validate_broker_account_selection(
    settings: Settings,
    account: "TradingAccount",
    connection: "BrokerConnection | None",
) -> None:
    """Fail closed when process credentials do not belong to the selected account."""
    if not account.active:
        raise BrokerConfigurationError("the selected trading account is archived")
    if connection is None:
        raise BrokerConfigurationError(
            "the selected account has no registered broker connection"
        )
    if connection.provider == "oanda-v20":
        expected_broker = "OANDA"
        expected_provider = "oanda-v20"
    elif connection.provider in {
        "metatrader-mt4-bridge",
        "metatrader-mt5-bridge",
    }:
        platform = "MT4" if "mt4" in connection.provider else "MT5"
        expected_broker = platform
        expected_provider = connection.provider
    else:
        raise BrokerConfigurationError(
            "registered broker connection provider is unsupported"
        )
    if account.broker.upper() != expected_broker:
        raise BrokerConfigurationError(
            f"selected account uses {account.broker}, but its connection requires "
            f"{expected_broker}"
        )
    if (
        connection.workspace_id != account.workspace_id
        or connection.account_id != account.id
        or connection.provider != expected_provider
    ):
        raise BrokerConfigurationError(
            "registered broker connection does not match the selected account"
        )
    if connection.status == "disabled":
        raise BrokerConfigurationError(
            "the selected account broker connection is disabled"
        )


def news_provider_configured(settings: Settings) -> bool:
    if settings.news_provider == "forex-factory":
        return True
    if settings.news_provider == "trading-economics":
        return bool(secret_value(settings.trading_economics_api_key))
    return False


def create_news_connector(
    settings: Settings,
) -> TradingEconomicsReadOnlyConnector | ForexFactoryReadOnlyConnector:
    if settings.news_provider == "forex-factory":
        return ForexFactoryReadOnlyConnector(
            timeout_seconds=settings.news_request_timeout_seconds,
            maximum_response_bytes=settings.news_max_response_bytes,
        )
    if settings.news_provider != "trading-economics":
        raise BrokerConfigurationError("NEWS_PROVIDER must select a news connector")
    api_key = secret_value(settings.trading_economics_api_key)
    if not api_key:
        raise BrokerConfigurationError(
            "TRADING_ECONOMICS_API_KEY is required for news and calendar reads"
        )
    return TradingEconomicsReadOnlyConnector(api_key)


def create_market_data_connector(
    settings: Settings,
    provider: str,
) -> OandaReadOnlyConnector | KrakenReadOnlyConnector | AlpacaReadOnlyConnector:
    normalized = provider.strip().lower()
    if normalized in {"oanda", "oanda-v20", "oanda-v2"}:
        if not settings.oanda_api_token or not settings.oanda_account_id:
            raise BrokerConfigurationError(
                "OANDA_API_TOKEN and OANDA_ACCOUNT_ID are required for OANDA market data"
            )
        return OandaReadOnlyConnector(
            token=secret_value(settings.oanda_api_token),
            account_id=secret_value(settings.oanda_account_id),
            environment=settings.oanda_environment,
            timeout_seconds=settings.oanda_request_timeout_seconds,
        )
    if normalized == "kraken":
        return KrakenReadOnlyConnector(
            base_url=settings.kraken_base_url,
            timeout_seconds=settings.kraken_request_timeout_seconds,
        )
    if normalized == "alpaca":
        key_id = secret_value(settings.alpaca_api_key_id)
        secret_key = secret_value(settings.alpaca_api_secret_key)
        if not key_id or not secret_key:
            raise BrokerConfigurationError(
                "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY are required for Alpaca "
                "market data"
            )
        return AlpacaReadOnlyConnector(
            key_id=key_id,
            secret_key=secret_key,
            base_url=settings.alpaca_data_base_url,
            timeout_seconds=settings.alpaca_request_timeout_seconds,
        )
    raise BrokerConfigurationError(
        f"market data provider is unknown or unsupported: {provider}"
    )
