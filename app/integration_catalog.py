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
        key="mt5",
        name="MetaTrader 5",
        status="adapter-only",
        capability="tick/candle normalization examples; no live terminal connector yet",
        setup="Requires the official MT5 terminal bridge; typically Windows-hosted",
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
