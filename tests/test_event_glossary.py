from app.services.event_glossary import event_insight


def test_us_event_glossary_explains_current_high_impact_releases() -> None:
    expected = {
        "Advance GDP q/q": "us-gdp",
        "Core PCE Price Index m/m": "us-core-pce",
        "Advance GDP Price Index q/q": "us-gdp-price-index",
        "Unemployment Claims": "us-unemployment-claims",
    }

    for title, key in expected.items():
        insight = event_insight(title, "USD")
        assert insight.key == key
        assert insight.source_url is not None
        assert insight.measures
        assert insight.why_markets_watch
        assert "XAU/USD" in insight.sensitive_markets[-1]
        assert "fixed direction" in insight.interpretation_caution


def test_unknown_event_fails_open_as_unclassified_reference() -> None:
    insight = event_insight("Novel experimental release", "EUR")

    assert insight.key == "unclassified-economic-event"
    assert insight.source_url is None
    assert insight.sensitive_markets == ("EUR markets",)
    assert "No reviewed definition" in insight.measures
