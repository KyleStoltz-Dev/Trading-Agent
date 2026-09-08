"""Session-scoped provider/model selection with conservative compatibility checks."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.providers.base import ModelProvider, ProviderConfigurationError, valid_model_id
from app.providers.catalog import supported_agent_model
from app.providers.factory import create_named_model_provider
from app.providers.ollama_provider import OllamaProvider
from app.services.model_credentials import model_api_key_configured
from app.services.secrets import SecretBackendError


@dataclass(frozen=True)
class ModelSelectionOption:
    provider: str
    model: str
    local: bool


class SessionModelController:
    """Own providers and cache model discovery for one interactive session."""

    def __init__(
        self,
        settings: Settings,
        provider: ModelProvider,
        *,
        cache_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        provider_factory: Callable[[Settings, str], ModelProvider] = (
            create_named_model_provider
        ),
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.model_override: str | None = None
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._provider_factory = provider_factory
        self._providers: dict[str, ModelProvider] = {provider.name: provider}
        self._model_cache: dict[str, tuple[float, tuple[str, ...]]] = {}

    @property
    def current_model(self) -> str:
        return self.model_override or self.provider.model

    def _provider_enabled(self, provider_name: str) -> bool:
        if provider_name == "ollama":
            return True
        try:
            return model_api_key_configured(
                self.settings,
                provider=provider_name,  # type: ignore[arg-type]
            )
        except SecretBackendError:
            return False

    def provider_for(self, provider_name: str) -> ModelProvider:
        provider = self._providers.get(provider_name)
        if provider is None:
            provider = self._provider_factory(self.settings, provider_name)
            self._providers[provider_name] = provider
        return provider

    def discover_models(self, provider_name: str) -> tuple[str, ...]:
        now = self._clock()
        cached = self._model_cache.get(provider_name)
        if cached is not None and cached[0] > now:
            return cached[1]

        provider = self.provider_for(provider_name)
        if isinstance(provider, OllamaProvider):
            discovered = tuple(
                sorted(
                    provider.installed_models(
                        timeout=self.settings.model_discovery_timeout_seconds
                    )
                )
            )
        else:
            available = getattr(provider, "available_models", None)
            discovered = tuple(available()) if available is not None else ()
        models = tuple(
            dict.fromkeys(
                model
                for model in discovered
                if supported_agent_model(provider_name, model)
            )
        )
        self._model_cache[provider_name] = (
            now + self._cache_ttl_seconds,
            models,
        )
        return models

    def options(self) -> tuple[ModelSelectionOption, ...]:
        options: list[ModelSelectionOption] = []
        provider_names = [
            name
            for name in ("ollama", "openai", "anthropic")
            if self._provider_enabled(name)
        ]
        available_names: list[str] = []
        for provider_name in provider_names:
            try:
                self.provider_for(provider_name)
            except ProviderConfigurationError:
                continue
            available_names.append(provider_name)
        with ThreadPoolExecutor(max_workers=max(1, len(available_names))) as executor:
            discoveries = {
                name: executor.submit(self.discover_models, name)
                for name in available_names
            }
            discovered_by_provider: dict[str, tuple[str, ...]] = {}
            for provider_name in available_names:
                try:
                    discovered_by_provider[provider_name] = discoveries[
                        provider_name
                    ].result()
                except ProviderConfigurationError:
                    discovered_by_provider[provider_name] = ()
        for provider_name in available_names:
            models = discovered_by_provider[provider_name]
            options.extend(
                ModelSelectionOption(
                    provider=provider_name,
                    model=model,
                    local=provider_name == "ollama",
                )
                for model in models
            )
        return tuple(options)

    def validate_selection(self, provider_name: str, model: str) -> ModelProvider:
        if provider_name not in {"ollama", "openai", "anthropic"}:
            raise ProviderConfigurationError(
                "Provider must be ollama, openai, or anthropic"
            )
        if not valid_model_id(model):
            raise ProviderConfigurationError(
                "Model name contains unsupported characters"
            )
        if not supported_agent_model(provider_name, model):
            raise ProviderConfigurationError(
                f"{provider_name}/{model} is not in the reviewed agent-compatible catalog"
            )
        provider = self.provider_for(provider_name)
        if model not in self.discover_models(provider_name):
            raise ProviderConfigurationError(
                f"{provider_name}/{model} is not available to the configured API key"
                if provider_name != "ollama"
                else f"{model} is not installed; run `trade models pull {model}`"
            )
        return provider

    def select(self, provider_name: str, model: str) -> ModelProvider:
        provider = self.validate_selection(provider_name, model)
        return self.activate(provider, model)

    def activate(self, provider: ModelProvider, model: str) -> ModelProvider:
        """Commit a provider/model pair that was already validated by the caller."""
        if self._providers.get(provider.name) is not provider:
            raise ProviderConfigurationError("model provider was not prepared by this session")
        self.provider = provider
        self.model_override = model
        return provider

    def automatic_profile(self) -> None:
        self.model_override = None

    def close(self) -> None:
        """Close each provider client once when the interactive session ends."""
        seen: set[int] = set()
        for provider in self._providers.values():
            client: Any = getattr(provider, "client", None)
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    continue
