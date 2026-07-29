import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventInsight:
    key: str
    measures: str
    why_markets_watch: str
    sensitive_markets: tuple[str, ...]
    interpretation_caution: str
    source_label: str | None
    source_url: str | None


_CAUTION = (
    "The reaction depends on the surprise versus consensus, revisions, positioning, "
    "and changes in policy expectations; the release does not imply a fixed direction."
)
_USD_MACRO_MARKETS = (
    "USD currency pairs",
    "U.S. Treasury yields",
    "U.S. equity-index futures",
    "XAU/USD through USD and real-rate expectations",
)


def event_insight(title: str, currency: str | None) -> EventInsight:
    normalized = " ".join(title.casefold().split())
    unit = (currency or "").strip().upper()

    if unit == "USD" and re.search(r"\bcore pce\b", normalized):
        return EventInsight(
            key="us-core-pce",
            measures=(
                "Changes in prices paid by U.S. consumers for goods and services, "
                "excluding food and energy."
            ),
            why_markets_watch=(
                "It is a widely followed measure of underlying inflation and can change "
                "expectations for Federal Reserve policy."
            ),
            sensitive_markets=_USD_MACRO_MARKETS,
            interpretation_caution=_CAUTION,
            source_label="U.S. Bureau of Economic Analysis — Core PCE",
            source_url="https://www.bea.gov/help/faq/518",
        )
    if unit == "USD" and re.search(r"\bgdp price index\b", normalized):
        return EventInsight(
            key="us-gdp-price-index",
            measures=(
                "Price changes across goods and services included in U.S. gross "
                "domestic product."
            ),
            why_markets_watch=(
                "It is a broad inflation measure released with GDP and can affect "
                "growth, inflation, and interest-rate expectations together."
            ),
            sensitive_markets=_USD_MACRO_MARKETS,
            interpretation_caution=_CAUTION,
            source_label="U.S. Bureau of Economic Analysis — Prices and inflation",
            source_url=(
                "https://www.bea.gov/resources/learning-center/"
                "what-to-know-prices-inflation"
            ),
        )
    if unit == "USD" and re.search(r"\b(?:advance |prelim |final )?gdp\b", normalized):
        return EventInsight(
            key="us-gdp",
            measures=(
                "The inflation-adjusted value of final goods and services produced "
                "in the United States; the advance release is the first estimate."
            ),
            why_markets_watch=(
                "It is a broad measure of economic growth and can change expectations "
                "for earnings, fiscal conditions, and Federal Reserve policy."
            ),
            sensitive_markets=_USD_MACRO_MARKETS,
            interpretation_caution=_CAUTION,
            source_label="U.S. Bureau of Economic Analysis — GDP",
            source_url="https://www.bea.gov/data/gdp/gross-domestic-product",
        )
    if unit == "USD" and re.search(
        r"\b(?:unemployment|jobless)(?: insurance)? claims\b",
        normalized,
    ):
        return EventInsight(
            key="us-unemployment-claims",
            measures=(
                "New claims filed for unemployment insurance during the reported week."
            ),
            why_markets_watch=(
                "It is a timely, high-frequency signal of labor-market layoffs and "
                "can shift expectations for growth and monetary policy."
            ),
            sensitive_markets=_USD_MACRO_MARKETS,
            interpretation_caution=_CAUTION,
            source_label="U.S. Department of Labor — Weekly UI claims",
            source_url=(
                "https://www.dol.gov/newsroom/releases"
                "?agency=39&state=All&topic=132&year=all"
            ),
        )
    if unit == "USD" and re.search(r"\bfederal funds rate\b|\bfomc\b", normalized):
        return EventInsight(
            key="us-fomc",
            measures=(
                "The Federal Reserve's policy decision and its communication about "
                "inflation, employment, and the expected policy path."
            ),
            why_markets_watch=(
                "The decision and guidance directly shape short-term rate expectations "
                "and discount rates across global markets."
            ),
            sensitive_markets=_USD_MACRO_MARKETS,
            interpretation_caution=(
                "The statement, projections, and press conference can matter more than "
                "the headline rate decision; market reaction is not mechanically directional."
            ),
            source_label="Federal Reserve — FOMC calendars",
            source_url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        )
    if unit == "USD" and re.search(r"\bcpi\b", normalized):
        return EventInsight(
            key="us-cpi",
            measures=(
                "Changes in prices paid by urban consumers for a basket of goods and services."
            ),
            why_markets_watch=(
                "It is a major inflation release and can rapidly change real-rate and "
                "Federal Reserve policy expectations."
            ),
            sensitive_markets=_USD_MACRO_MARKETS,
            interpretation_caution=_CAUTION,
            source_label="U.S. Bureau of Labor Statistics — CPI",
            source_url="https://www.bls.gov/cpi/",
        )
    if unit == "USD" and re.search(r"\bdurable goods orders\b", normalized):
        return EventInsight(
            key="us-durable-goods",
            measures=(
                "New orders placed with U.S. manufacturers for long-lasting goods; "
                "core readings exclude volatile transportation orders."
            ),
            why_markets_watch=(
                "Orders can provide an early signal of business investment and future "
                "manufacturing activity."
            ),
            sensitive_markets=_USD_MACRO_MARKETS,
            interpretation_caution=_CAUTION,
            source_label="U.S. Census Bureau — Manufacturers' shipments and orders",
            source_url="https://www.census.gov/manufacturing/m3/",
        )

    return EventInsight(
        key="unclassified-economic-event",
        measures="No reviewed definition is stored for this event yet.",
        why_markets_watch=(
            "Use the source release and the event's currency, impact, forecast, and "
            "revision history before drawing a conclusion."
        ),
        sensitive_markets=((f"{unit} markets",) if unit else ()),
        interpretation_caution=_CAUTION,
        source_label=None,
        source_url=None,
    )
