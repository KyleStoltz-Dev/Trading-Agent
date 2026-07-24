from typing import Any

from app.config import Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ModelProvider, ProviderConfigurationError
from app.providers.openai_provider import OpenAIProvider


def resolve_provider_name(settings: Settings) -> str:
    if settings.model_provider != "auto":
        return settings.model_provider

    available = [
        name
        for name, configured in (
            ("openai", bool(settings.openai_api_key)),
            ("anthropic", bool(settings.anthropic_api_key)),
        )
        if configured
    ]
    if len(available) == 1:
        return available[0]
    if not available:
        raise ProviderConfigurationError(
            "Configure OPENAI_API_KEY or ANTHROPIC_API_KEY, then select MODEL_PROVIDER"
        )
    raise ProviderConfigurationError(
        "Both provider keys are configured; set MODEL_PROVIDER=openai or anthropic"
    )


def create_model_provider(settings: Settings, client: Any = None) -> ModelProvider:
    provider_name = resolve_provider_name(settings)
    if provider_name == "openai":
        if not settings.openai_api_key and client is None:
            raise ProviderConfigurationError("OPENAI_API_KEY is required for the OpenAI provider")
        return OpenAIProvider(settings, client=client)
    if provider_name == "anthropic":
        if not settings.anthropic_api_key and client is None:
            raise ProviderConfigurationError(
                "ANTHROPIC_API_KEY is required for the Anthropic provider"
            )
        return AnthropicProvider(settings, client=client)
    raise ProviderConfigurationError(f"Unsupported model provider: {provider_name}")
