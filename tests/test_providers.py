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
from app.system_resources import GIB, ResourceSnapshot


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
            SimpleNamespace(
                output=[tool_call],
                output_text="",
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=10,
                    input_tokens_details=SimpleNamespace(cached_tokens=20),
                ),
            ),
            SimpleNamespace(
                output=[],
                output_text="done",
                usage=SimpleNamespace(
                    input_tokens=50,
                    output_tokens=20,
                    input_tokens_details=SimpleNamespace(cached_tokens=10),
                ),
            ),
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
    assert responses.calls[0]["max_output_tokens"] == 900
    assert responses.calls[0]["store"] is False
    assert provider.last_usage.input_tokens == 150
    assert provider.last_usage.output_tokens == 30
    assert provider.last_usage.cached_input_tokens == 30
    output = next(
        item
        for item in responses.calls[1]["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    assert json.loads(output["output"]) == {"ok": True}


def test_nested_chart_analysis_is_included_in_total_usage() -> None:
    tool_call = SimpleNamespace(
        type="function_call",
        name="analyze_chart",
        call_id="chart-1",
        arguments="{}",
    )

    def usage(input_tokens, output_tokens):
        return SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        )

    responses = FakeResponses(
        [
            SimpleNamespace(output=[tool_call], output_text="", usage=usage(100, 10)),
            SimpleNamespace(
                output=[],
                output_text='{"visible_facts":[]}',
                usage=usage(200, 20),
            ),
            SimpleNamespace(output=[], output_text="done", usage=usage(50, 5)),
        ]
    )
    provider = OpenAIProvider(
        Settings(openai_api_key="test"),
        client=SimpleNamespace(responses=responses),
    )

    def execute_tool(name, arguments):
        del name, arguments
        return json.dumps(
            provider.analyze_chart(
                image_bytes=b"image",
                content_type="image/png",
                user_context="review",
                instructions="facts only",
                output_schema={
                    "type": "object",
                    "properties": {
                        "visible_facts": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["visible_facts"],
                },
            )
        )

    assert (
        provider.complete(
            instructions="rules",
            message="analyze",
            history=[],
            tools=[],
            execute_tool=execute_tool,
            max_tool_rounds=2,
        )
        == "done"
    )
    assert provider.last_usage.input_tokens == 350
    assert provider.last_usage.output_tokens == 35
    assert responses.calls[1]["store"] is False


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
            SimpleNamespace(
                content=[tool_call],
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=10,
                    cache_read_input_tokens=20,
                    cache_creation_input_tokens=5,
                ),
            ),
            SimpleNamespace(
                content=[text],
                usage=SimpleNamespace(
                    input_tokens=50,
                    output_tokens=20,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            ),
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
    assert messages.calls[0]["max_tokens"] == 900
    assert messages.calls[0]["thinking"]["type"] == "adaptive"
    assert messages.calls[0]["output_config"]["effort"] == "medium"
    continuation = messages.calls[1]["messages"]
    assert continuation[-2]["role"] == "assistant"
    assert continuation[-1]["role"] == "user"
    assert continuation[-1]["content"][0]["type"] == "tool_result"
    assert continuation[-1]["content"][0]["tool_use_id"] == "tool-1"
    assert provider.last_usage.input_tokens == 175
    assert provider.last_usage.cached_input_tokens == 20
    assert provider.last_usage.cache_write_input_tokens == 5
    assert provider.last_usage.output_tokens == 30
    assert provider.last_usage.cached_input_tokens == 20


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
                    },
                    "prompt_eval_count": 20,
                    "eval_count": 5,
                },
            )
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "done"},
                "prompt_eval_count": 10,
                "eval_count": 7,
                "total_duration": 2_000_000_000,
                "load_duration": 500_000_000,
                "prompt_eval_duration": 1_000_000_000,
                "eval_duration": 1_000_000_000,
            },
        )

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(
        Settings(model_provider="ollama", resource_aware_model_routing=False),
        client=client,
    )
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
    assert requests[0]["options"]["num_predict"] == 2300
    assert requests[0]["options"]["temperature"] == 0.2
    assert requests[0]["tools"][0]["function"]["name"] == "calculate_position_size"
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_name": "calculate_position_size",
        "content": '{"ok": true}',
    }
    assert provider.last_usage.input_tokens == 30
    assert provider.last_usage.output_tokens == 12
    assert provider.last_performance["output_tokens_per_second"] == 7
    assert provider.last_performance["load_seconds"] == 0.5
    client.close()


def test_ollama_retries_without_thinking_when_reasoning_uses_the_generation_cap() -> None:
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
                        "thinking": "Reasoning consumed the available generation.",
                    },
                    "prompt_eval_count": 20,
                    "eval_count": 1800,
                },
            )
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "Compact answer."},
                "prompt_eval_count": 20,
                "eval_count": 20,
            },
        )

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(
        Settings(model_provider="ollama", resource_aware_model_routing=False),
        client=client,
    )

    result = provider.complete(
        instructions="Be concise.",
        message="Answer.",
        history=[],
        tools=[],
        execute_tool=lambda name, arguments: "{}",
        max_tool_rounds=1,
        model="local-model",
        reasoning_effort="low",
        max_output_tokens=400,
    )

    assert result == "Compact answer."
    assert requests[0]["think"] == "low"
    assert requests[0]["options"]["num_predict"] == 1800
    assert requests[1]["think"] is False
    assert requests[1]["options"]["num_predict"] == 400
    assert provider.last_usage.input_tokens == 40
    assert provider.last_usage.output_tokens == 1820
    client.close()


def test_ollama_can_unload_a_model_before_switching() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"done": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(
        Settings(model_provider="ollama", resource_aware_model_routing=False),
        client=client,
    )

    provider.unload_model("qwen3.5:9b")

    assert captured == {
        "model": "qwen3.5:9b",
        "stream": False,
        "keep_alive": 0,
    }
    client.close()


def test_ollama_reports_installed_sizes_and_loaded_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3.5:9b", "size": 6_600_000_000},
                        {"name": "qwen3.5:35b-a3b", "size": 24_000_000_000},
                    ]
                },
            )
        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3.5:9b"}]},
            )
        return httpx.Response(404)

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(
        Settings(model_provider="ollama", resource_aware_model_routing=False),
        client=client,
    )

    assert provider.installed_model_sizes() == {
        "qwen3.5:9b": 6_600_000_000,
        "qwen3.5:35b-a3b": 24_000_000_000,
    }
    assert provider.loaded_models() == frozenset({"qwen3.5:9b"})
    client.close()


def test_ollama_provider_blocks_unsafe_inference_at_the_shared_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3.5:35b-a3b", "size": 24 * GIB}]},
            )
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/chat":
            chat_calls += 1
            return httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": "READY"}},
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        "app.providers.ollama_provider.resource_snapshot",
        lambda: ResourceSnapshot(
            platform="TestOS",
            total_memory_bytes=16 * GIB,
            available_memory_bytes=12 * GIB,
            memory_percent=25,
            swap_total_bytes=None,
            swap_used_bytes=None,
            swap_percent=None,
            disk_free_bytes=100 * GIB,
        ),
    )
    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(
        Settings(
            model_provider="ollama",
            ollama_model="qwen3.5:35b-a3b",
            ollama_runtime_lock_path=tmp_path / "ollama.lock",
        ),
        client=client,
    )

    with pytest.raises(RuntimeError, match="resource guard"):
        provider.smoke_test()

    assert chat_calls == 0
    client.close()


def test_remote_ollama_does_not_use_client_resource_telemetry_or_unload() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "READY"}},
        )

    client = httpx.Client(
        base_url="https://ollama.example",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(
        Settings(
            model_provider="ollama",
            ollama_base_url="https://ollama.example",
            ollama_allow_remote=True,
        ),
        client=client,
    )

    assert provider.smoke_test() == "READY"
    assert requests == ["/api/chat"]
    with pytest.raises(ProviderConfigurationError, match="MANAGE_REMOTE"):
        provider.unload_model("qwen3.5:9b")
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
    provider = OllamaProvider(
        Settings(model_provider="ollama", resource_aware_model_routing=False),
        client=client,
    )

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
    provider = OllamaProvider(
        Settings(model_provider="ollama", resource_aware_model_routing=False),
        client=client,
    )

    assert provider.smoke_test() == "READY"
    assert captured["think"] is False
    assert captured["keep_alive"] == 0
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


def test_remote_ollama_requires_tls_or_separate_insecure_override() -> None:
    with pytest.raises(ProviderConfigurationError, match="requires HTTPS"):
        OllamaProvider(
            Settings(
                model_provider="ollama",
                ollama_base_url="http://remote.example:11434",
                ollama_allow_remote=True,
            )
        )

    provider = OllamaProvider(
        Settings(
            model_provider="ollama",
            ollama_base_url="http://remote.example:11434",
            ollama_allow_remote=True,
            ollama_allow_insecure_remote=True,
        )
    )
    provider.client.close()


def test_ollama_rejects_installed_model_digest_drift() -> None:
    expected = "sha256:" + ("a" * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3.5:9b",
                            "size": 6_600_000_000,
                            "digest": "sha256:" + ("b" * 64),
                        }
                    ]
                },
            )
        return httpx.Response(500)

    client = httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaProvider(
        Settings(
            model_provider="ollama",
            ollama_model_digests=f"qwen3.5:9b={expected}",
            resource_aware_model_routing=False,
        ),
        client=client,
    )

    with pytest.raises(ProviderConfigurationError, match="does not match"):
        provider.smoke_test()
    client.close()


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
    provider = OllamaProvider(
        Settings(model_provider="ollama", resource_aware_model_routing=False),
        client=client,
    )
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
