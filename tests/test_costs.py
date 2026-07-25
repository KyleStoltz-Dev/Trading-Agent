from datetime import date
from decimal import Decimal

from app.costs import (
    TokenUsage,
    calculate_cost,
    format_usd,
    model_pricing,
    output_budget_for_mode,
)


def test_openai_model_prices_and_cost_calculation() -> None:
    pricing = model_pricing("openai", "gpt-5.6-terra")

    assert pricing is not None
    assert pricing.input_per_million == Decimal("2.50")
    assert pricing.output_per_million == Decimal("15.00")
    cost = calculate_cost(
        pricing,
        TokenUsage(
            input_tokens=10_000,
            cached_input_tokens=2_000,
            output_tokens=1_000,
        ),
    )
    assert cost == Decimal("0.035500")
    assert format_usd(cost) == "$0.04"


def test_anthropic_introductory_price_expires_automatically() -> None:
    introductory = model_pricing(
        "anthropic",
        "claude-sonnet-5",
        as_of=date(2026, 8, 31),
    )
    standard = model_pricing(
        "anthropic",
        "claude-sonnet-5",
        as_of=date(2026, 9, 1),
    )

    assert introductory is not None
    assert standard is not None
    assert introductory.input_per_million == Decimal("2")
    assert introductory.output_per_million == Decimal("10")
    assert standard.input_per_million == Decimal("3")
    assert standard.output_per_million == Decimal("15")


def test_local_models_have_zero_api_cost() -> None:
    pricing = model_pricing("ollama", "qwen3.5:9b")

    assert pricing is not None
    assert calculate_cost(
        pricing,
        TokenUsage(input_tokens=100_000, output_tokens=10_000),
    ) == Decimal("0")
    assert output_budget_for_mode("deep") > output_budget_for_mode("economy")


def test_unknown_model_does_not_guess_a_price() -> None:
    assert model_pricing("openai", "future-unknown-model") is None
