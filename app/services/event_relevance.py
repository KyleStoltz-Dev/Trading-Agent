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


def instrument_event_currencies(instrument: str) -> frozenset[str]:
    """Derive relevant fiat currencies without treating metals/crypto as calendars."""
    candidates: set[str] = set()
    for group in re.findall(r"[A-Z]+", instrument.upper()):
        if len(group) == 3:
            candidates.add(group)
        elif len(group) == 6:
            candidates.update((group[:3], group[3:]))
        elif len(group) > 3:
            candidates.add(group[-3:])
    return frozenset(candidates & ECONOMIC_EVENT_CURRENCIES)
