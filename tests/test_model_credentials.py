from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import app.cli as cli_module
from app.config import Settings
from app.providers.factory import create_named_model_provider
from app.providers.openai_provider import OpenAIProvider
from app.services import model_credentials
from app.services.model_credentials import (
    model_api_key_configured,
    remove_model_api_key,
    resolve_model_credentials,
    store_model_api_key,
)
from app.services.secrets import SecretBackendError


class MemorySecretBackend:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}

    def get(self, reference: str):
        return self.values.get(reference)

    def put(self, reference: str, values) -> None:
        self.values[reference] = dict(values)

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


def test_model_api_key_round_trip_uses_vault_without_exposing_key(monkeypatch) -> None:
    backend = MemorySecretBackend()
    monkeypatch.setattr(model_credentials, "secret_backend", lambda _settings: backend)
    settings = Settings(model_provider="openai")

    store_model_api_key(settings, provider="openai", api_key="  private-api-key  ")

    assert backend.values == {
        "keyring:model/openai": {"api_key": "private-api-key"}
    }
    assert model_api_key_configured(settings, provider="openai")
    assert resolve_model_credentials(settings, provider="openai").api_key == "private-api-key"

    remove_model_api_key(settings, provider="openai")
    assert not model_api_key_configured(settings, provider="openai")


def test_environment_model_key_takes_precedence_over_vault(monkeypatch) -> None:
    backend = MemorySecretBackend()
    backend.values["keyring:model/openai"] = {"api_key": "vault-key"}
    monkeypatch.setattr(model_credentials, "secret_backend", lambda _settings: backend)
    settings = Settings(openai_api_key=SecretStr("environment-key"))

    credentials = resolve_model_credentials(settings, provider="openai")

    assert credentials is not None
    assert credentials.api_key == "environment-key"


def test_model_api_key_rejects_empty_or_control_characters(monkeypatch) -> None:
    monkeypatch.setattr(
        model_credentials,
        "secret_backend",
        lambda _settings: MemorySecretBackend(),
    )
    settings = Settings()

    with pytest.raises(SecretBackendError, match="malformed"):
        store_model_api_key(settings, provider="anthropic", api_key="short")
    with pytest.raises(SecretBackendError, match="malformed"):
        store_model_api_key(
            settings,
            provider="anthropic",
            api_key="long-enough\nleak",
        )


def test_named_provider_accepts_injected_client_without_reading_keyring() -> None:
    client = SimpleNamespace()

    provider = create_named_model_provider(Settings(), "openai", client=client)

    assert isinstance(provider, OpenAIProvider)
    assert provider.client is client


def test_openai_model_discovery_filters_non_conversational_models() -> None:
    client = SimpleNamespace(
        models=SimpleNamespace(
            list=lambda: SimpleNamespace(
                data=[
                    SimpleNamespace(id="gpt-5.6-terra"),
                    SimpleNamespace(id="gpt-image-2"),
                    SimpleNamespace(id="text-embedding-3-small"),
                ]
            )
        )
    )
    provider = OpenAIProvider(Settings(openai_model="gpt-5.6-sol"), client=client)

    assert provider.available_models() == ("gpt-5.6-sol", "gpt-5.6-terra")


def test_model_menu_combines_local_and_configured_cloud_models(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "model_api_key_configured",
        lambda _settings, *, provider: provider == "openai",
    )
    monkeypatch.setattr(
        cli_module,
        "_discover_provider_models",
        lambda _settings, provider: {
            "ollama": ("qwen3.5:9b",),
            "openai": ("gpt-5.6-terra",),
        }[provider],
    )

    options = cli_module._model_menu_options(
        Settings(model_provider="ollama"),
        current_provider="ollama",
        current_model="qwen3.5:9b",
    )

    assert [option.value for option in options] == [
        "ollama\0qwen3.5:9b",
        "openai\0gpt-5.6-terra",
    ]
    assert "current" in options[0].description
