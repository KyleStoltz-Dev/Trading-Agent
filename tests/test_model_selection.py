from types import SimpleNamespace
from unittest.mock import Mock

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


def test_hosted_conversation_confirmation_is_plain_and_provider_specific(
    monkeypatch,
) -> None:
    prompts = []
    monkeypatch.setattr(
        cli_module.typer,
        "confirm",
        lambda prompt: prompts.append(prompt) or True,
    )

    assert cli_module._confirm_agent_external_action(
        "External disclosure: hosted conversation",
        {
            "provider": "openai",
            "destination": "hosted-provider:openai",
            "conversation_turns": 2,
            "content": "bounded history",
        },
    )

    assert prompts == [
        "Use OpenAI for this conversation? Recent chat context and future requests "
        "will be sent to that provider."
    ]


def test_model_browse_uses_full_screen_browser_with_complete_status(monkeypatch) -> None:
    controller = SimpleNamespace(
        settings=Settings(
            model_provider="openai",
            openai_auth_mode="subscription",
        ),
        options=Mock(
            return_value=(
                SimpleNamespace(
                    provider="openai",
                    model="gpt-5.6-sol",
                    local=False,
                    access_mode="subscription",
                ),
                SimpleNamespace(
                    provider="openai",
                    model="gpt-5.6-terra",
                    local=False,
                    access_mode="subscription",
                ),
            )
        ),
    )
    browser = Mock(return_value="openai\0gpt-5.6-terra")
    monkeypatch.setattr(cli_module, "choose_terminal_option", browser)
    monkeypatch.setattr(cli_module, "choose_inline_terminal_option", Mock())
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli_module.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        cli_module,
        "claude_subscription_status",
        lambda: SimpleNamespace(ready=False, detail="Run `claude auth login`."),
    )
    monkeypatch.setattr(cli_module, "model_api_key_configured", Mock(return_value=False))

    selected = cli_module._choose_session_model(
        controller,
        current_provider="openai",
        current_model="gpt-5.6-sol",
        browse=True,
    )

    assert selected == ("openai", "gpt-5.6-terra")
    assert browser.call_args.args[0] == "Model browser"
    summary = browser.call_args.args[1]
    assert "Current: openai/gpt-5.6-sol" in summary
    assert "ChatGPT: 2 model(s) · subscription" in summary
    assert "Claude: not connected" in summary
    assert "claude-sonnet-5" in summary
    assert "subscriptions are included" in summary
    assert "Provider billing is authoritative." in summary
    labels = [option.label for option in browser.call_args.args[2]]
    assert labels == [
        "ChatGPT subscription · gpt-5.6-sol · Included with plan",
        "ChatGPT subscription · gpt-5.6-terra · Included with plan",
    ]
    assert browser.call_args.kwargs == {
        "show_descriptions": False,
        "default": "openai\0gpt-5.6-sol",
    }


def test_model_browser_cost_labels_are_simple_and_access_aware() -> None:
    assert cli_module._model_cost_label("ollama", "qwen3.5:9b", "local") == (
        "No API charge"
    )
    assert cli_module._model_cost_label(
        "openai", "gpt-5.6-sol", "subscription"
    ) == "Included with plan"
    assert cli_module._model_cost_label("openai", "gpt-5.6-luna", "api") == (
        "$1 in / $6 out per 1M"
    )
    assert cli_module._model_cost_label("openai", "unknown-model", "api") == (
        "Price unavailable"
    )


def test_bare_mode_uses_inline_picker_with_plain_descriptions(monkeypatch) -> None:
    picker = Mock(return_value="deep")
    monkeypatch.setattr(cli_module, "choose_inline_terminal_option", picker)
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli_module.sys, "stdout", SimpleNamespace(isatty=lambda: True))

    selected = cli_module._choose_session_mode("balanced")

    assert selected == "deep"
    assert picker.call_args.args[0] == "Mode ❯ "
    options = picker.call_args.args[1]
    assert [option.value for option in options] == [
        "auto",
        "economy",
        "balanced",
        "deep",
    ]
    assert options[0].label == "Auto (recommended)"
    assert options[2].description.startswith("current · ")
    assert cli_module._mode_confirmation("auto") == (
        "Mode set to Auto. Effort will adapt to each request."
    )
    assert cli_module._mode_confirmation("balanced") == (
        "Mode set to Balanced (medium effort)."
    )


def test_mode_browse_explains_routing_profiles_and_permissions(monkeypatch) -> None:
    browser = Mock(return_value="economy")
    monkeypatch.setattr(cli_module, "choose_terminal_option", browser)
    monkeypatch.setattr(cli_module, "choose_inline_terminal_option", Mock())
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli_module.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    settings = Settings(
        model_provider="openai",
        openai_model="gpt-5.6-sol",
        openai_economy_model="gpt-5.6-luna",
        openai_balanced_model="gpt-5.6-terra",
        openai_deep_model="gpt-5.6-sol",
    )

    selected = cli_module._choose_session_mode(
        "balanced",
        browse=True,
        settings=settings,
        provider_name="openai",
        access_mode="subscription",
    )

    assert selected == "economy"
    assert browser.call_args.args[0] == "Mode browser"
    summary = browser.call_args.args[1]
    assert "Current mode: Balanced" in summary
    assert "Provider: ChatGPT subscription" in summary
    assert "Routine requests and journaling → Economy" in summary
    assert "Normal chart and trade analysis → Balanced" in summary
    assert "Research, comparisons, and backtests → Deep" in summary
    assert "Economy (low effort) → gpt-5.6-luna" in summary
    assert "Balanced (medium effort) → gpt-5.6-terra" in summary
    assert "Deep (high effort) → gpt-5.6-sol" in summary
    assert "not trading permissions" in summary
    assert browser.call_args.kwargs == {
        "show_descriptions": False,
        "default": "balanced",
    }


def test_mode_browse_explains_session_model_override() -> None:
    summary = cli_module._mode_browser_summary(
        Settings(model_provider="anthropic"),
        current_mode="deep",
        provider_name="anthropic",
        access_mode="subscription",
        model_override="claude-sonnet-5",
    )

    assert "Provider: Claude subscription" in summary
    assert "Session model override: claude-sonnet-5" in summary
    assert "All modes keep this model; only reasoning effort changes." in summary
    assert "Configured model profiles:" not in summary
