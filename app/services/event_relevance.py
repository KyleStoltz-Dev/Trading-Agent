import re

ECONOMIC_EVENT_CURRENCIES = frozenset(
    {
        "AUD",
        "CAD",
        "CHF",
        "CNY",
        "EUR",
        "GBP",
        "HKD",
        "JPY",
        "MXN",
        "NZD",
        "SEK",
        "SGD",
        "USD",
        "ZAR",
    }
)

_MARKET_ALIASES = {
    "GOLD": frozenset({"USD"}),
    "SILVER": frozenset({"USD"}),
    "XAU": frozenset({"USD"}),
    "XAG": frozenset({"USD"}),
    "SPX": frozenset({"USD"}),
    "SP500": frozenset({"USD"}),
    "NASDAQ": frozenset({"USD"}),
    "NAS100": frozenset({"USD"}),
    "US30": frozenset({"USD"}),
}


def instrument_event_currencies(instrument: str) -> frozenset[str]:
    """Derive relevant fiat currencies without treating metals/crypto as calendars."""
    candidates: set[str] = set()
    normalized = instrument.upper()
    for alias, currencies in _MARKET_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            candidates.update(currencies)
    for group in re.findall(r"[A-Z]+", normalized):
        candidates.update(_MARKET_ALIASES.get(group, ()))
        if len(group) == 3:
            candidates.add(group)
        elif len(group) == 6:
            candidates.update((group[:3], group[3:]))
        elif len(group) > 3:
            candidates.add(group[-3:])
    return frozenset(candidates & ECONOMIC_EVENT_CURRENCIES)
