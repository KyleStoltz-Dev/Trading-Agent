from typing import Any

from app.config import Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ModelProvider, ProviderConfigurationError
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.subscription_provider import (
    ClaudeSubscriptionProvider,
    CodexSubscriptionProvider,
    claude_subscription_status,
    codex_subscription_status,
)
from app.services.model_credentials import resolve_model_credentials
from app.services.secrets import SecretBackendError


def resolve_provider_name(settings: Settings) -> str:
    if settings.model_provider != "auto":
        return settings.model_provider

    environment_providers = [
        name
        for name, configured in (
            ("openai", bool(settings.openai_api_key)),
            ("anthropic", bool(settings.anthropic_api_key)),
        )
        if configured
    ]
    if len(environment_providers) == 1:
        return environment_providers[0]
    if len(environment_providers) > 1:
        raise ProviderConfigurationError(
            "Both provider keys are configured; set MODEL_PROVIDER=openai or anthropic"
        )

    try:
        available = [
            name
            for name in ("openai", "anthropic")
            if resolve_model_credentials(
                settings,
                provider=name,  # type: ignore[arg-type]
            )
            is not None
        ]
    except SecretBackendError as exc:
        raise ProviderConfigurationError(str(exc)) from exc
    if len(available) == 1:
        return available[0]
    if not available:
        return "ollama"
    raise ProviderConfigurationError(
        "Both provider keys are configured; set MODEL_PROVIDER=openai or anthropic"
    )


def create_model_provider(settings: Settings, client: Any = None) -> ModelProvider:
    provider_name = resolve_provider_name(settings)
    return create_named_model_provider(settings, provider_name, client=client)


def create_named_model_provider(
    settings: Settings,
    provider_name: str,
    client: Any = None,
) -> ModelProvider:
    if provider_name == "openai":
        auth_mode = settings.openai_auth_mode
        if client is None and auth_mode != "api":
            status = codex_subscription_status()
            if status.ready:
                return CodexSubscriptionProvider(settings)
            if auth_mode == "subscription":
                raise ProviderConfigurationError(status.detail)
        try:
            credentials = (
                None
                if client is not None
                else resolve_model_credentials(settings, provider="openai")
            )
        except SecretBackendError as exc:
            raise ProviderConfigurationError(str(exc)) from exc
        if credentials is None and client is None:
            raise ProviderConfigurationError(
                "OpenAI is not connected. Run `codex login` for ChatGPT access, or "
                "run `trade setup --provider openai` to use an API key."
            )
        return OpenAIProvider(
            settings,
            client=client,
            api_key=credentials.api_key if credentials else None,
        )
    if provider_name == "anthropic":
        auth_mode = settings.anthropic_auth_mode
        if client is None and auth_mode != "api":
            status = claude_subscription_status()
            if status.ready:
                return ClaudeSubscriptionProvider(settings)
            if auth_mode == "subscription":
                raise ProviderConfigurationError(status.detail)
        try:
            credentials = (
                None
                if client is not None
                else resolve_model_credentials(settings, provider="anthropic")
            )
        except SecretBackendError as exc:
            raise ProviderConfigurationError(str(exc)) from exc
        if credentials is None and client is None:
            raise ProviderConfigurationError(
                "Claude is not connected. Run `claude auth login` for subscription "
                "access, or run `trade setup --provider anthropic` to use an API key."
            )
        return AnthropicProvider(
            settings,
            client=client,
            api_key=credentials.api_key if credentials else None,
        )
    if provider_name == "ollama":
        return OllamaProvider(settings, client=client)
    raise ProviderConfigurationError(f"Unsupported model provider: {provider_name}")
