import json
from types import SimpleNamespace

from app.config import Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.factory import resolve_provider_name
from app.providers.openai_provider import OpenAIProvider


class FakeResponses:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.outputs)


class FakeMessages:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.outputs)


def test_provider_auto_selection_uses_the_only_configured_key() -> None:
    assert (
        resolve_provider_name(
            Settings(
                model_provider="auto",
                openai_api_key="test",
                anthropic_api_key=None,
            )
        )
        == "openai"
    )
    assert (
        resolve_provider_name(
            Settings(
                model_provider="auto",
                openai_api_key=None,
                anthropic_api_key="test",
            )
        )
        == "anthropic"
    )


def test_openai_adapter_returns_tool_output_to_responses_api() -> None:
    tool_call = SimpleNamespace(
        type="function_call",
        name="calculate_position_size",
        call_id="call-1",
        arguments=json.dumps({"value": "input"}),
    )
    responses = FakeResponses(
        [
            SimpleNamespace(output=[tool_call], output_text=""),
            SimpleNamespace(output=[], output_text="done"),
        ]
    )
    provider = OpenAIProvider(
        Settings(openai_api_key="test"),
        client=SimpleNamespace(responses=responses),
    )

    result = provider.complete(
        instructions="rules",
        message="calculate",
        history=[],
        tools=[],
        execute_tool=lambda name, arguments: json.dumps({"ok": True}),
        max_tool_rounds=2,
        model="economy-model",
        reasoning_effort="low",
    )

    assert result == "done"
    assert responses.calls[0]["model"] == "economy-model"
    assert responses.calls[0]["reasoning"]["effort"] == "low"
    assert responses.calls[0]["store"] is False
    output = next(
        item
        for item in responses.calls[1]["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    assert json.loads(output["output"]) == {"ok": True}


def test_anthropic_adapter_returns_tool_result_immediately_after_tool_use() -> None:
    tool_call = SimpleNamespace(
        type="tool_use",
        id="tool-1",
        name="calculate_position_size",
        input={"value": "input"},
        model_dump=lambda mode: {
            "type": "tool_use",
            "id": "tool-1",
            "name": "calculate_position_size",
            "input": {"value": "input"},
        },
    )
    text = SimpleNamespace(type="text", text="done")
    messages = FakeMessages(
        [
            SimpleNamespace(content=[tool_call]),
            SimpleNamespace(content=[text]),
        ]
    )
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test"),
        client=SimpleNamespace(messages=messages),
    )

    result = provider.complete(
        instructions="rules",
        message="calculate",
        history=[],
        tools=[],
        execute_tool=lambda name, arguments: json.dumps({"ok": True}),
        max_tool_rounds=2,
    )

    assert result == "done"
    continuation = messages.calls[1]["messages"]
    assert continuation[-2]["role"] == "assistant"
    assert continuation[-1]["role"] == "user"
    assert continuation[-1]["content"][0]["type"] == "tool_result"
    assert continuation[-1]["content"][0]["tool_use_id"] == "tool-1"
