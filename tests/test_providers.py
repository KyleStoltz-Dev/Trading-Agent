import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ProviderConfigurationError
from app.providers.factory import create_model_provider, resolve_provider_name
from app.providers.ollama_provider import OllamaProvider
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


def test_ollama_adapter_runs_tools_and_returns_the_follow_up_text() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "calculate_position_size",
                                    "arguments": {"value": "input"},
                                }
                            }
                        ],
                    }
                },
            )
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "done"}},
        )

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(Settings(model_provider="ollama"), client=client)
    result = provider.complete(
        instructions="rules",
        message="calculate",
        history=[],
        tools=[
            {
                "type": "function",
                "name": "calculate_position_size",
                "description": "Calculate risk.",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        execute_tool=lambda name, arguments: json.dumps({"ok": True}),
        max_tool_rounds=2,
        model="local-model",
        reasoning_effort="low",
    )

    assert result == "done"
    assert requests[0]["model"] == "local-model"
    assert requests[0]["think"] == "low"
    assert requests[0]["tools"][0]["function"]["name"] == "calculate_position_size"
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_name": "calculate_position_size",
        "content": '{"ok": true}',
    }
    client.close()


def test_ollama_chart_analysis_uses_images_and_json_schema() -> None:
    captured: dict = {}
    schema = {
        "type": "object",
        "properties": {"visible_facts": {"type": "array", "items": {"type": "string"}}},
        "required": ["visible_facts"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"visible_facts": ["Price bars are visible."]}),
                }
            },
        )

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(Settings(model_provider="ollama"), client=client)

    result = provider.analyze_chart(
        image_bytes=b"image",
        content_type="image/png",
        user_context="Review this chart.",
        instructions="Only visible facts.",
        output_schema=schema,
    )

    assert result == {"visible_facts": ["Price bars are visible."]}
    assert captured["format"] == schema
    assert captured["messages"][1]["images"]
    assert captured["options"]["temperature"] == 0
    client.close()


def test_ollama_smoke_test_generates_a_small_response() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "READY"}},
        )

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(Settings(model_provider="ollama"), client=client)

    assert provider.smoke_test() == "READY"
    assert captured["think"] is False
    assert captured["options"]["num_predict"] == 8
    assert captured["options"]["num_ctx"] == 2048
    client.close()


def test_ollama_requires_explicit_opt_in_for_remote_hosts() -> None:
    with pytest.raises(ProviderConfigurationError, match="OLLAMA_ALLOW_REMOTE"):
        OllamaProvider(
            Settings(
                model_provider="ollama",
                ollama_base_url="http://remote.example:11434",
            )
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:11434?token=secret",
        "http://127.0.0.1:11434#fragment",
    ],
)
def test_ollama_rejects_secrets_and_extras_in_base_url(url: str) -> None:
    with pytest.raises(ProviderConfigurationError, match="credentials"):
        OllamaProvider(
            Settings(
                model_provider="ollama",
                ollama_base_url=url,
            )
        )


def test_ollama_handles_invalid_tool_arguments_without_running_the_tool() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "calculate_position_size",
                                    "arguments": "{not-json",
                                }
                            }
                        ],
                    }
                },
            )
        payload = json.loads(request.content)
        assert json.loads(payload["messages"][-1]["content"]) == {
            "ok": False,
            "error": "invalid Ollama tool call",
        }
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "recovered"}},
        )

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(Settings(model_provider="ollama"), client=client)
    result = provider.complete(
        instructions="rules",
        message="calculate",
        history=[],
        tools=[],
        execute_tool=lambda name, arguments: pytest.fail("tool should not run"),
        max_tool_rounds=2,
    )

    assert result == "recovered"
    client.close()


def test_factory_creates_ollama_without_a_model_api_key() -> None:
    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": "ok"}},
            )
        ),
    )
    provider = create_model_provider(
        Settings(
            model_provider="ollama",
            openai_api_key=None,
            anthropic_api_key=None,
        ),
        client=client,
    )

    assert provider.name == "ollama"
    client.close()
