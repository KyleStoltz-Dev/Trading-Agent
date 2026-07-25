import base64
import importlib
from typing import Any

from app.config import Settings, secret_value
from app.costs import TokenUsage
from app.providers.base import (
    ProviderConfigurationError,
    ToolExecutor,
    record_analysis_usage,
    safe_tool_error,
    track_completion_usage,
)


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
            "strict": tool.get("strict", False),
        }
        for tool in tools
    ]


def _content_dict(block: Any) -> dict[str, Any]:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json")
    if isinstance(block, dict):
        return block
    raise TypeError(f"Unsupported Anthropic content block: {type(block).__name__}")


def _anthropic_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    base_input = int(getattr(usage, "input_tokens", 0) or 0)
    return TokenUsage(
        input_tokens=base_input + cached + cache_write,
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cached_input_tokens=cached,
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.anthropic_model
        self.last_usage = TokenUsage()
        if client is None:
            try:
                anthropic = importlib.import_module("anthropic")
            except ImportError as exc:
                raise ProviderConfigurationError(
                    'Install the Anthropic adapter with `pip install -e ".[anthropic]"`'
                ) from exc
            client = anthropic.Anthropic(
                api_key=secret_value(settings.anthropic_api_key)
            )
        self.client = client

    @track_completion_usage
    def complete(
        self,
        *,
        instructions: str,
        message: str,
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        execute_tool: ToolExecutor,
        max_tool_rounds: int,
        model: str | None = None,
        reasoning_effort: str = "medium",
    ) -> str:
        messages: list[dict[str, Any]] = [*history, {"role": "user", "content": message}]
        provider_tools = _anthropic_tools(tools)

        for _ in range(max_tool_rounds):
            response = self.client.messages.create(
                model=model or self.model,
                max_tokens=4096,
                system=instructions,
                messages=messages,
                tools=provider_tools,
            )
            self.last_usage += _anthropic_usage(response)
            tool_calls = [block for block in response.content if block.type == "tool_use"]
            if not tool_calls:
                return "\n".join(
                    block.text for block in response.content if block.type == "text"
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": [_content_dict(block) for block in response.content],
                }
            )
            results = []
            for call in tool_calls:
                try:
                    output = execute_tool(call.name, call.input)
                    is_error = False
                except Exception as exc:
                    output = safe_tool_error(exc)
                    is_error = True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": output,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
        raise RuntimeError("agent exceeded the maximum tool-call rounds")

    def analyze_chart(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        user_context: str,
        instructions: str,
        output_schema: dict[str, Any],
        model: str | None = None,
        reasoning_effort: str = "medium",
    ) -> dict[str, Any]:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        tool_name = "return_chart_analysis"
        response = self.client.messages.create(
            model=model or self.model,
            max_tokens=4096,
            system=instructions,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": content_type,
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": user_context},
                    ],
                }
            ],
            tools=[
                {
                    "name": tool_name,
                    "description": "Return the chart analysis in the required schema.",
                    "input_schema": output_schema,
                    "strict": True,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
        record_analysis_usage(self, _anthropic_usage(response))
        block = next(
            (
                item
                for item in response.content
                if item.type == "tool_use" and item.name == tool_name
            ),
            None,
        )
        if block is None:
            raise RuntimeError("Anthropic did not return structured chart analysis")
        return block.input
