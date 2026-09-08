from types import SimpleNamespace

import pytest

import app.cli as cli_module
from app.config import Settings
from app.providers.base import ProviderConfigurationError
from app.services.model_selection import SessionModelController


@pytest.mark.parametrize(
    ("message", "matches_mode"),
    (
        ("/mode", True),
        ("/mode deep", True),
        ("/model", False),
        ("/model use openai/gpt-5.6-sol", False),
        ("/modeled", False),
        ("", False),
    ),
)
def test_mode_command_matches_only_the_complete_command_token(
    message: str,
    matches_mode: bool,
) -> None:
    assert cli_module._matches_chat_command(message, "/mode") is matches_mode


class FakeProvider:
    def __init__(self, name: str, model: str, models: tuple[str, ...] = ()) -> None:
        self.name = name
        self.model = model
        self.models = models
        self.discovery_calls = 0
        self.client = SimpleNamespace(close=self._close)
        self.closed = False

    def _close(self) -> None:
        self.closed = True

    def available_models(self) -> tuple[str, ...]:
        self.discovery_calls += 1
        return self.models


def test_session_model_controller_caches_discovery_and_closes_clients() -> None:
    provider = FakeProvider(
        "openai",
        "gpt-5.6-sol",
        ("gpt-5.6-terra",),
    )
    now = [10.0]
    controller = SessionModelController(
        Settings(model_provider="openai", openai_api_key="test-key"),
        provider,
        cache_ttl_seconds=30,
        clock=lambda: now[0],
    )

    assert controller.discover_models("openai") == ("gpt-5.6-terra",)
    assert controller.discover_models("openai") == ("gpt-5.6-terra",)
    assert provider.discovery_calls == 1

    now[0] = 41.0
    controller.discover_models("openai")
    assert provider.discovery_calls == 2

    controller.close()
    assert provider.closed


def test_session_model_controller_rejects_unreviewed_cloud_model() -> None:
    provider = FakeProvider("openai", "gpt-5.6-sol")
    controller = SessionModelController(
        Settings(model_provider="openai", openai_api_key="test-key"),
        provider,
    )

    with pytest.raises(ProviderConfigurationError, match="reviewed"):
        controller.validate_selection("openai", "gpt-4o")


def test_session_model_controller_rejects_unavailable_reviewed_cloud_model() -> None:
    provider = FakeProvider("openai", "gpt-5.6-sol", ("gpt-5.6-terra",))
    controller = SessionModelController(
        Settings(model_provider="openai", openai_api_key="test-key"),
        provider,
    )

    with pytest.raises(ProviderConfigurationError, match="not available"):
        controller.validate_selection("openai", "gpt-5.6-luna")


def test_same_hosted_provider_model_switch_needs_no_new_disclosure(monkeypatch) -> None:
    hosted = FakeProvider(
        "openai",
        "gpt-5.6-sol",
        ("gpt-5.6-sol", "gpt-5.6-terra"),
    )
    controller = SessionModelController(
        Settings(model_provider="openai", openai_api_key="test-key"),
        hosted,
    )
    confirmation = []
    monkeypatch.setattr(
        cli_module,
        "_confirm_agent_external_action",
        lambda *args: confirmation.append(args) or True,
    )

    result = cli_module._switch_session_model(
        Settings(model_provider="openai", openai_api_key="test-key"),
        controller,
        provider_name="openai",
        model="gpt-5.6-terra",
        last_runtime_model=None,
        conversation_turns=4,
    )

    assert result == (hosted, None)
    assert controller.current_model == "gpt-5.6-terra"
    assert confirmation == []


def test_hosted_switch_discloses_history_and_can_be_declined(monkeypatch) -> None:
    local = FakeProvider("ollama", "qwen3.5:9b")
    hosted = FakeProvider("openai", "gpt-5.6-sol", ("gpt-5.6-sol",))
    controller = SessionModelController(
        Settings(model_provider="ollama"),
        local,
        provider_factory=lambda _settings, _name: hosted,
    )
    confirmation = []

    def decline(action, payload):
        confirmation.append((action, payload))
        return False

    monkeypatch.setattr(cli_module, "_confirm_agent_external_action", decline)

    result = cli_module._switch_session_model(
        Settings(model_provider="ollama"),
        controller,
        provider_name="openai",
        model="gpt-5.6-sol",
        last_runtime_model=None,
        conversation_turns=7,
    )

    assert result is None
    assert controller.provider is local
    assert confirmation[0][0] == "External disclosure: hosted conversation"
    assert confirmation[0][1]["conversation_turns"] == 7


def test_hosted_switch_updates_provider_after_confirmation(monkeypatch) -> None:
    local = FakeProvider("ollama", "qwen3.5:9b")
    hosted = FakeProvider(
        "anthropic",
        "claude-sonnet-5",
        ("claude-sonnet-5",),
    )
    controller = SessionModelController(
        Settings(model_provider="ollama"),
        local,
        provider_factory=lambda _settings, _name: hosted,
    )
    monkeypatch.setattr(
        cli_module,
        "_confirm_agent_external_action",
        lambda _action, _payload: True,
    )

    result = cli_module._switch_session_model(
        Settings(model_provider="ollama"),
        controller,
        provider_name="anthropic",
        model="claude-sonnet-5",
        last_runtime_model=None,
        conversation_turns=2,
    )

    assert result == (hosted, None)
    assert controller.provider is hosted
    assert controller.model_override == "claude-sonnet-5"
