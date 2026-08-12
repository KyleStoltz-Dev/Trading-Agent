"""Read-only external connector adapters."""

from app.connectors.factory import (
    BrokerConfigurationError,
    create_broker_connector,
    create_market_data_connector,
    create_metatrader_connector,
    create_news_connector,
    create_oanda_connector,
    news_provider_configured,
)
from app.connectors.metatrader_bridge import (
    MetaTraderBridgeError,
    MetaTraderReadOnlyBridgeConnector,
)
from app.connectors.oanda import OandaConnectorError, OandaReadOnlyConnector

__all__ = [
    "BrokerConfigurationError",
    "MetaTraderBridgeError",
    "MetaTraderReadOnlyBridgeConnector",
    "OandaConnectorError",
    "OandaReadOnlyConnector",
    "create_broker_connector",
    "create_market_data_connector",
    "create_metatrader_connector",
    "create_oanda_connector",
    "create_news_connector",
    "news_provider_configured",
]
