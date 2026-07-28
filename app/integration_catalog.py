from dataclasses import dataclass


@dataclass(frozen=True)
class IntegrationOption:
    kind: str
    key: str
    name: str
    status: str
    capability: str
    setup: str
    documentation: str


INTEGRATIONS = (
    IntegrationOption(
        kind="broker",
        key="oanda",
        name="OANDA v20",
        status="ready",
        capability="read-only quotes, candles, account, positions, fills, and reconciliation",
        setup="OANDA_API_TOKEN, OANDA_ACCOUNT_ID, OANDA_ENVIRONMENT=practice",
        documentation="https://developer.oanda.com/rest-live-v20/introduction/",
    ),
    IntegrationOption(
        kind="broker",
        key="metatrader",
        name="MetaTrader 4 / 5 bridge",
        status="ready",
        capability=(
            "read-only live data and execution ingestion; included Windows MT5 service "
            "and a shared MT4 bridge contract"
        ),
        setup=(
            "METATRADER_BRIDGE_URL, METATRADER_BRIDGE_TOKEN, "
            "METATRADER_ACCOUNT_ID, METATRADER_PLATFORM"
        ),
        documentation="https://www.mql5.com/en/docs/python_metatrader5",
    ),
    IntegrationOption(
        kind="broker",
        key="ctrader",
        name="cTrader Open API",
        status="planned",
        capability="view-only account scope, live/historical market data, positions and deals",
        setup="Future OAuth integration will request accounts scope, never trading scope",
        documentation="https://help.ctrader.com/open-api/account-authentication/",
    ),
    IntegrationOption(
        kind="broker",
        key="ibkr",
        name="Interactive Brokers",
        status="planned",
        capability="multi-asset market/account data",
        setup="Future read-only Client Portal/Web API integration",
        documentation="https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/",
    ),
    IntegrationOption(
        kind="news",
        key="trading-economics",
        name="Trading Economics",
        status="ready",
        capability="economic calendar and news with provider/source timestamps",
        setup="TRADING_ECONOMICS_API_KEY",
        documentation="https://docs.tradingeconomics.com/get_started/",
    ),
    IntegrationOption(
        kind="chart-alert",
        key="tradingview",
        name="TradingView webhooks",
        status="ready",
        capability=(
            "verified, replay-safe chart alerts as read-only evidence; "
            "never broker execution"
        ),
        setup=(
            "Public HTTPS proxy with TradingView mTLS/source-IP verification; "
            "then TRADINGVIEW_WEBHOOK_ENABLED=true"
        ),
        documentation=(
            "https://www.tradingview.com/support/solutions/43000529348-"
            "how-to-configure-webhook-alerts/"
        ),
    ),
    IntegrationOption(
        kind="news",
        key="finnhub",
        name="Finnhub",
        status="planned",
        capability="forex headlines; economic calendar requires an eligible paid plan",
        setup="Future FINNHUB_API_KEY adapter",
        documentation="https://finnhub.io/docs/api",
    ),
)


def integration_options(kind: str | None = None) -> tuple[IntegrationOption, ...]:
    return tuple(
        option for option in INTEGRATIONS if kind is None or option.kind == kind
    )
