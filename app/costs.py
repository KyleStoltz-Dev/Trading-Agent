import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

MILLION = Decimal("1000000")


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_input_tokens=(
                self.cache_write_input_tokens + other.cache_write_input_tokens
            ),
        )


@dataclass(frozen=True)
class ModelPricing:
    provider: str
    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None
    source_url: str
    note: str = ""
    cache_write_input_per_million: Decimal | None = None


OPENAI_PRICES = {
    "gpt-5.6-sol": ("5.00", "30.00", "0.50"),
    "gpt-5.6": ("5.00", "30.00", "0.50"),
    "gpt-5.6-terra": ("2.50", "15.00", "0.25"),
    "gpt-5.6-luna": ("1.00", "6.00", "0.10"),
}
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/models/compare"
ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"


def model_pricing(
    provider: str,
    model: str,
    *,
    as_of: date | None = None,
) -> ModelPricing | None:
    normalized = model.lower()
    if provider == "ollama":
        return ModelPricing(
            provider=provider,
            model=model,
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
            cached_input_per_million=Decimal("0"),
            source_url="https://ollama.com/",
            note="local inference; electricity and hardware costs are not included",
        )
    if provider == "openai" and normalized in OPENAI_PRICES:
        input_rate, output_rate, cached_rate = OPENAI_PRICES[normalized]
        return ModelPricing(
            provider=provider,
            model=model,
            input_per_million=Decimal(input_rate),
            output_per_million=Decimal(output_rate),
            cached_input_per_million=Decimal(cached_rate),
            source_url=OPENAI_PRICING_URL,
            note="standard processing below the long-context pricing threshold",
        )
    if provider == "anthropic" and normalized == "claude-sonnet-5":
        current = as_of or date.today()
        introductory = current <= date(2026, 8, 31)
        return ModelPricing(
            provider=provider,
            model=model,
            input_per_million=Decimal("2" if introductory else "3"),
            output_per_million=Decimal("10" if introductory else "15"),
            cached_input_per_million=(
                Decimal("0.2") if introductory else Decimal("0.3")
            ),
            source_url=ANTHROPIC_PRICING_URL,
            note=(
                "introductory pricing through 2026-08-31"
                if introductory
                else "standard pricing after 2026-08-31"
            ),
            cache_write_input_per_million=(
                Decimal("2.5") if introductory else Decimal("3.75")
            ),
        )
    return None


def approximate_tokens(value: Any) -> int:
    serialized = value if isinstance(value, str) else json.dumps(value, default=str)
    return max(1, math.ceil(len(serialized) / 4))


def estimated_request_tokens(
    *,
    instructions: str,
    message: str,
    history: list[dict[str, str]],
    tools: list[dict[str, Any]],
) -> int:
    return approximate_tokens(
        {
            "instructions": instructions,
            "history": history,
            "message": message,
            "tools": tools,
        }
    )


def output_budget_for_mode(mode: str) -> int:
    return {
        "economy": 400,
        "balanced": 900,
        "deep": 1800,
    }.get(mode, 900)


def estimated_multi_round_usage(
    *,
    initial_input_tokens: int,
    output_tokens_per_round: int,
    rounds: int,
    tool_result_tokens_per_round: int = 1000,
) -> TokenUsage:
    """Estimate growing-context usage for a bounded tool-calling request."""
    bounded_rounds = max(1, rounds)
    prior_round_context = (
        output_tokens_per_round + max(0, tool_result_tokens_per_round)
    )
    growing_context = (
        prior_round_context * bounded_rounds * (bounded_rounds - 1) // 2
    )
    return TokenUsage(
        input_tokens=(initial_input_tokens * bounded_rounds) + growing_context,
        output_tokens=output_tokens_per_round * bounded_rounds,
    )


def calculate_cost(pricing: ModelPricing, usage: TokenUsage) -> Decimal:
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    cache_write = min(
        usage.cache_write_input_tokens,
        usage.input_tokens - cached,
    )
    regular = usage.input_tokens - cached - cache_write
    cached_rate = pricing.cached_input_per_million or pricing.input_per_million
    cache_write_rate = (
        pricing.cache_write_input_per_million or pricing.input_per_million
    )
    return (
        Decimal(regular) * pricing.input_per_million
        + Decimal(cached) * cached_rate
        + Decimal(cache_write) * cache_write_rate
        + Decimal(usage.output_tokens) * pricing.output_per_million
    ) / MILLION


def format_usd(value: Decimal) -> str:
    if value == 0:
        return "$0"
    if value < Decimal("0.01"):
        return f"${value.quantize(Decimal('0.0001'))}"
    return f"${value.quantize(Decimal('0.01'))}"


def format_pricing(pricing: ModelPricing) -> str:
    if pricing.provider == "ollama":
        return "$0 API cost (local)"
    return (
        f"${pricing.input_per_million}/M input · "
        f"${pricing.output_per_million}/M output"
    )
