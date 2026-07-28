"""Per-account broker secret storage.

PostgreSQL stores only opaque references. Local installations use the operating-system
credential vault through ``keyring``; hosted deployments inject an external backend.
"""

import importlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.config import (
    HOSTED_VAULT_BACKEND,
    LEGACY_ENV_BACKEND,
    LOCAL_VAULT_BACKEND,
    Settings,
    secret_value,
)

_REFERENCE = re.compile(r"^(keyring|external):[A-Za-z0-9._:/-]{1,240}$")
_REQUIRED_FIELDS = {
    "oanda-v20": frozenset({"token"}),
    "metatrader-mt4-bridge": frozenset({"token"}),
    "metatrader-mt5-bridge": frozenset({"token"}),
}


class SecretBackendError(RuntimeError):
    pass


@runtime_checkable
class SecretBackend(Protocol):
    """Contract implemented by local keyring and hosted secret managers."""

    def get(self, reference: str) -> Mapping[str, str] | None: ...

    def put(self, reference: str, values: Mapping[str, str]) -> None: ...

    def delete(self, reference: str) -> None: ...


@dataclass(frozen=True)
class BrokerCredentials:
    token: str


class KeyringSecretBackend:
    service_name = "trading-agent"

    @staticmethod
    def _keyring():
        try:
            return importlib.import_module("keyring")
        except ImportError as exc:
            raise SecretBackendError(
                "OS credential storage requires the `keyring` package; reinstall "
                "Trading Agent with current dependencies"
            ) from exc

    def get(self, reference: str) -> Mapping[str, str] | None:
        value = self._keyring().get_password(self.service_name, reference)
        if value is None:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SecretBackendError("credential vault entry is malformed") from exc
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in decoded.items()
        ):
            raise SecretBackendError("credential vault entry is malformed")
        return decoded

    def put(self, reference: str, values: Mapping[str, str]) -> None:
        self._keyring().set_password(
            self.service_name,
            reference,
            json.dumps(dict(values), sort_keys=True, separators=(",", ":")),
        )

    def delete(self, reference: str) -> None:
        keyring = self._keyring()
        try:
            keyring.delete_password(self.service_name, reference)
        except keyring.errors.PasswordDeleteError:
            return


def _external_backend(settings: Settings) -> SecretBackend:
    target = settings.broker_external_secret_backend
    if not target or ":" not in target:
        raise SecretBackendError(
            "BROKER_EXTERNAL_SECRET_BACKEND must be MODULE:ATTRIBUTE"
        )
    module_name, attribute = target.split(":", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module_name) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", attribute
    ):
        raise SecretBackendError("external secret backend import path is invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
        backend = factory(settings) if callable(factory) else factory
    except Exception as exc:
        raise SecretBackendError("external secret backend could not be loaded") from exc
    if not isinstance(backend, SecretBackend):
        raise SecretBackendError("external secret backend does not implement the contract")
    return backend


def secret_backend(settings: Settings) -> SecretBackend:
    if settings.broker_secret_backend == LOCAL_VAULT_BACKEND:
        return KeyringSecretBackend()
    if settings.broker_secret_backend == HOSTED_VAULT_BACKEND:
        return _external_backend(settings)
    raise SecretBackendError("legacy environment credentials do not support secret writes")


def validate_secret_backend(settings: Settings) -> None:
    """Verify backend availability without reading or exposing any credential."""
    if settings.broker_secret_backend == LOCAL_VAULT_BACKEND:
        keyring = KeyringSecretBackend._keyring()
        backend = keyring.get_keyring()
        if getattr(backend, "priority", 0) <= 0:
            raise SecretBackendError(
                "no usable operating-system credential vault is available"
            )
        return
    if settings.broker_secret_backend == HOSTED_VAULT_BACKEND:
        _external_backend(settings)
        return
    if settings.deployment_mode != "local-single-user":
        raise SecretBackendError("legacy environment credentials are local-only")


def new_secret_reference(settings: Settings) -> str:
    prefix = (
        LOCAL_VAULT_BACKEND
        if settings.broker_secret_backend == LOCAL_VAULT_BACKEND
        else HOSTED_VAULT_BACKEND
    )
    if prefix == HOSTED_VAULT_BACKEND and not settings.broker_external_secret_backend:
        raise SecretBackendError("external secret backend is not configured")
    return f"{prefix}:broker/{uuid.uuid4()}"


def store_broker_secret(
    settings: Settings,
    *,
    reference: str,
    provider: str,
    token: str,
) -> None:
    if not _REFERENCE.fullmatch(reference):
        raise SecretBackendError("broker secret reference is invalid")
    if provider not in _REQUIRED_FIELDS:
        raise SecretBackendError("unsupported broker provider")
    normalized = token.strip()
    if not normalized or (provider.startswith("metatrader-") and len(normalized) < 32):
        raise SecretBackendError("broker token does not meet provider requirements")
    secret_backend(settings).put(reference, {"token": normalized})


def remove_broker_secret(settings: Settings, reference: str) -> None:
    if not _REFERENCE.fullmatch(reference):
        raise SecretBackendError("broker secret reference is invalid")
    secret_backend(settings).delete(reference)


def resolve_broker_credentials(
    settings: Settings,
    *,
    provider: str,
    reference: str | None,
) -> BrokerCredentials:
    if settings.broker_secret_backend == LEGACY_ENV_BACKEND:
        if settings.deployment_mode != "local-single-user":
            raise SecretBackendError("legacy environment credentials are local-only")
        expected = (
            "env:OANDA_API_TOKEN"
            if provider == "oanda-v20"
            else "env:METATRADER_BRIDGE_TOKEN"
        )
        if reference != expected:
            raise SecretBackendError("legacy credential reference does not match provider")
        value = (
            secret_value(settings.oanda_api_token)
            if provider == "oanda-v20"
            else secret_value(settings.metatrader_bridge_token)
        )
        if not value:
            raise SecretBackendError("legacy broker credential is not configured")
        return BrokerCredentials(token=value)
    if reference is None or not _REFERENCE.fullmatch(reference):
        raise SecretBackendError("account has no valid broker secret reference")
    expected_prefix = f"{settings.broker_secret_backend}:"
    if not reference.startswith(expected_prefix):
        raise SecretBackendError("broker secret reference uses a different backend")
    values = secret_backend(settings).get(reference)
    if values is None:
        raise SecretBackendError("broker credential was not found")
    required = _REQUIRED_FIELDS.get(provider)
    if required is None or not required.issubset(values):
        raise SecretBackendError("broker credential is incomplete")
    return BrokerCredentials(token=values["token"])
