from app.config import Settings, secret_value
from app.connectors.oanda import OandaReadOnlyConnector
from app.connectors.trading_economics import TradingEconomicsReadOnlyConnector


class BrokerConfigurationError(ValueError):
    pass


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


def create_news_connector(settings: Settings) -> TradingEconomicsReadOnlyConnector:
    api_key = secret_value(settings.trading_economics_api_key)
    if not api_key:
        raise BrokerConfigurationError(
            "TRADING_ECONOMICS_API_KEY is required for news and calendar reads"
        )
    return TradingEconomicsReadOnlyConnector(api_key)
