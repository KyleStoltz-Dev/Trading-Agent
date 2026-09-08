import json
import subprocess
from types import SimpleNamespace

import pytest

import app.providers.factory as factory_module
import app.providers.subscription_provider as subscription_module
from app.config import Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.subscription_provider import (
    ClaudeSubscriptionProvider,
    CodexSubscriptionProvider,
    SubscriptionRuntimeStatus,
    claude_subscription_status,
    codex_subscription_status,
)


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_codex_status_requires_chatgpt_sign_in(monkeypatch) -> None:
    monkeypatch.setattr(subscription_module.shutil, "which", lambda _name: "/bin/codex")

    status = codex_subscription_status(
        runner=lambda _command, **_kwargs: completed("Logged in using ChatGPT")
    )

    assert status.ready
    assert status.detail == "Signed in with ChatGPT"


@pytest.mark.parametrize(
    ("payload", "ready"),
    (
        ({"loggedIn": True, "authMethod": "oauth", "subscriptionType": "max"}, True),
        ({"loggedIn": True, "authMethod": "api_key"}, False),
    ),
)
def test_claude_status_distinguishes_subscription_from_api_key(
    monkeypatch,
    payload: dict[str, object],
    ready: bool,
) -> None:
    monkeypatch.setattr(subscription_module.shutil, "which", lambda _name: "/bin/claude")

    status = claude_subscription_status(
        runner=lambda _command, **_kwargs: completed(json.dumps(payload))
    )

    assert status.ready is ready
    assert status.authenticated


def test_factory_prefers_subscription_but_api_mode_is_an_override(monkeypatch) -> None:
    sentinel = SimpleNamespace(name="anthropic", access_mode="subscription")
    monkeypatch.setattr(
        factory_module,
        "claude_subscription_status",
        lambda: SubscriptionRuntimeStatus(True, True, True, "ready"),
    )
    monkeypatch.setattr(
        factory_module,
        "ClaudeSubscriptionProvider",
        lambda _settings: sentinel,
    )

    selected = factory_module.create_named_model_provider(
        Settings(anthropic_api_key="test-key"),
        "anthropic",
    )
    api = factory_module.create_named_model_provider(
        Settings(anthropic_auth_mode="api", anthropic_api_key="test-key"),
        "anthropic",
        client=SimpleNamespace(),
    )

    assert selected is sentinel
    assert isinstance(api, AnthropicProvider)
    assert api.access_mode == "api"


def test_codex_subscription_tool_request_uses_trading_agent_executor(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    responses = iter(
        (
            {
                "kind": "tool",
                "message": "",
                "tool_name": "lookup_journal",
                "arguments_json": '{"limit": 3}',
            },
            {
                "kind": "final",
                "message": "I found three journal entries.",
                "tool_name": "",
                "arguments_json": "",
            },
        )
    )

    def runner(command, **kwargs):
        calls.append({"command": command, **kwargs})
        response = next(responses)
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": json.dumps(response)},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 4},
                    }
                ),
            )
        )
        return completed(stdout)

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    provider = CodexSubscriptionProvider(Settings(), runner=runner)
    executions = []

    reply = provider.complete(
        instructions="Be useful.",
        message="Show my last entries",
        history=[],
        tools=[
            {
                "name": "lookup_journal",
                "description": "Read journal entries",
                "parameters": {"type": "object"},
            }
        ],
        execute_tool=lambda name, arguments: executions.append((name, arguments))
        or '[{"id": 1}]',
        max_tool_rounds=3,
    )

    assert reply == "I found three journal entries."
    assert executions == [("lookup_journal", {"limit": 3})]
    assert provider.last_usage.input_tokens == 20
    assert all("OPENAI_API_KEY" not in call["env"] for call in calls)
    assert all("shell_tool" in call["command"] for call in calls)


def test_subscription_provider_rejects_undeclared_tool() -> None:
    responses = iter(
        (
            {
                "kind": "tool",
                "message": "",
                "tool_name": "place_order",
                "arguments_json": "{}",
            },
            {
                "kind": "final",
                "message": "That action is unavailable.",
                "tool_name": "",
                "arguments_json": "",
            },
        )
    )

    def runner(_command, **_kwargs):
        response = next(responses)
        event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(response)},
        }
        return completed(json.dumps(event))

    provider = CodexSubscriptionProvider(Settings(), runner=runner)
    executed = []
    reply = provider.complete(
        instructions="Be safe.",
        message="Trade now",
        history=[],
        tools=[],
        execute_tool=lambda name, arguments: executed.append((name, arguments)) or "",
        max_tool_rounds=2,
    )

    assert reply == "That action is unavailable."
    assert executed == []


def test_codex_subscription_chart_analysis_attaches_temporary_image() -> None:
    captured = {}

    def runner(command, **kwargs):
        image_path = command[command.index("--image") + 1]
        captured["image_exists_during_call"] = subscription_module.Path(image_path).exists()
        captured["command"] = command
        event = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps({"instrument": "unknown"}),
            },
        }
        return completed(json.dumps(event))

    provider = CodexSubscriptionProvider(Settings(), runner=runner)
    result = provider.analyze_chart(
        image_bytes=b"synthetic-image",
        content_type="image/png",
        user_context="Describe only visible evidence.",
        instructions="Do not invent labels.",
        output_schema={
            "type": "object",
            "properties": {"instrument": {"type": "string"}},
            "required": ["instrument"],
            "additionalProperties": False,
        },
    )

    assert result == {"instrument": "unknown"}
    assert captured["image_exists_during_call"] is True
    assert "--image" in captured["command"]


def test_claude_subscription_parses_structured_output_and_strips_api_key(
    monkeypatch,
) -> None:
    captured = {}

    def runner(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return completed(
            json.dumps(
                {
                    "structured_output": {
                        "kind": "final",
                        "message": "Ready.",
                        "tool_name": "",
                        "arguments_json": "",
                    },
                    "usage": {"input_tokens": 8, "output_tokens": 2},
                }
            )
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    provider = ClaudeSubscriptionProvider(Settings(), runner=runner)
    reply = provider.complete(
        instructions="Be useful.",
        message="Hello",
        history=[],
        tools=[],
        execute_tool=lambda _name, _arguments: "",
        max_tool_rounds=1,
    )

    assert reply == "Ready."
    assert provider.last_usage.input_tokens == 8
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "--safe-mode" in captured["command"]
    assert captured["command"][captured["command"].index("--tools") + 1] == ""


def test_subscription_rate_limit_error_is_plain() -> None:
    provider = CodexSubscriptionProvider(
        Settings(),
        runner=lambda _command, **_kwargs: completed(
            stderr='RateLimitError 429 {"secret":"noise"}',
            returncode=1,
        ),
    )

    with pytest.raises(RuntimeError, match="subscription limit") as error:
        provider.complete(
            instructions="Be useful.",
            message="Hello",
            history=[],
            tools=[],
            execute_tool=lambda _name, _arguments: "",
            max_tool_rounds=1,
        )

    assert "secret" not in str(error.value)
