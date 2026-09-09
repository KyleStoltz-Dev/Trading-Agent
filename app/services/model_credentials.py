"""Secure model-provider credentials for local and hosted installations."""

from dataclasses import dataclass
from typing import Literal

from app.config import LEGACY_ENV_BACKEND, Settings, secret_value
from app.services.secrets import SecretBackendError, secret_backend

CloudModelProvider = Literal["openai", "anthropic"]
_SUPPORTED = frozenset({"openai", "anthropic"})


@dataclass(frozen=True)
class ModelCredentials:
    api_key: str


def _reference(settings: Settings, provider: str) -> str:
    if provider not in _SUPPORTED:
        raise SecretBackendError("unsupported cloud model provider")
    if settings.broker_secret_backend == LEGACY_ENV_BACKEND:
        raise SecretBackendError("legacy environment credentials do not support secret writes")
    return f"{settings.broker_secret_backend}:model/{provider}"


def _environment_key(settings: Settings, provider: str) -> str | None:
    if provider == "openai":
        return secret_value(settings.openai_api_key)
    if provider == "anthropic":
        return secret_value(settings.anthropic_api_key)
    raise SecretBackendError("unsupported cloud model provider")


def store_model_api_key(
    settings: Settings,
    *,
    provider: CloudModelProvider,
    api_key: str,
) -> None:
    normalized = api_key.strip()
    if len(normalized) < 8 or any(character in normalized for character in "\r\n\0"):
        raise SecretBackendError("model API key is empty or malformed")
    secret_backend(settings).put(_reference(settings, provider), {"api_key": normalized})


def remove_model_api_key(settings: Settings, *, provider: CloudModelProvider) -> None:
    secret_backend(settings).delete(_reference(settings, provider))


def resolve_model_credentials(
    settings: Settings,
    *,
    provider: CloudModelProvider,
) -> ModelCredentials | None:
    if settings.broker_secret_backend == LEGACY_ENV_BACKEND:
        environment_key = _environment_key(settings, provider)
        return ModelCredentials(api_key=environment_key) if environment_key else None
    values = secret_backend(settings).get(_reference(settings, provider))
    if values is not None:
        api_key = values.get("api_key", "").strip()
        if not api_key:
            raise SecretBackendError("model credential vault entry is incomplete")
        return ModelCredentials(api_key=api_key)
    environment_key = _environment_key(settings, provider)
    return ModelCredentials(api_key=environment_key) if environment_key else None


def model_api_key_configured(
    settings: Settings,
    *,
    provider: CloudModelProvider,
) -> bool:
    return resolve_model_credentials(settings, provider=provider) is not None
