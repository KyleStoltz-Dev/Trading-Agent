import base64
import importlib
import json
from typing import Any

from app.config import Settings, secret_value
from app.costs import TokenUsage
from app.providers.base import (
    ProviderConfigurationError,
    ToolExecutor,
    limit_provider_capacity,
    provider_capacity_limiter,
    record_analysis_usage,
    safe_tool_error,
    track_completion_usage,
)


def _openai_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    details = getattr(usage, "input_tokens_details", None)
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cached_input_tokens=int(getattr(details, "cached_tokens", 0) or 0),
    )


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.openai_model
        self.last_usage = TokenUsage()
        self.safety_identifier = settings.openai_safety_identifier
        self._capacity_limiter = provider_capacity_limiter(
            self.name,
            settings.model_max_concurrent_requests,
        )
        self._capacity_queue_timeout_seconds = (
            settings.model_request_queue_timeout_seconds
        )
        if client is None:
            try:
                openai = importlib.import_module("openai")
            except ImportError as exc:
                raise ProviderConfigurationError(
                    'Install the OpenAI adapter with `pip install -e ".[openai]"`'
                ) from exc
            client = openai.OpenAI(api_key=secret_value(settings.openai_api_key))
        self.client = client

    @track_completion_usage
    @limit_provider_capacity
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
        max_output_tokens: int = 900,
    ) -> str:
        input_items: list[Any] = [*history, {"role": "user", "content": message}]
        for _ in range(max_tool_rounds):
            response = self.client.responses.create(
                model=model or self.model,
                instructions=instructions,
                input=input_items,
                tools=tools,
                reasoning={"effort": reasoning_effort, "context": "current_turn"},
                max_output_tokens=max_output_tokens,
                safety_identifier=self.safety_identifier,
                store=False,
            )
            self.last_usage += _openai_usage(response)
            tool_calls = [item for item in response.output if item.type == "function_call"]
            if not tool_calls:
                return response.output_text

            input_items.extend(response.output)
            for call in tool_calls:
                try:
                    output = execute_tool(call.name, json.loads(call.arguments))
                except Exception as exc:
                    output = json.dumps(
                        {
                            "ok": False,
                            "error": safe_tool_error(exc),
                        }
                    )
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": output,
                    }
                )
        raise RuntimeError("agent exceeded the maximum tool-call rounds")

    @limit_provider_capacity
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
        response = self.client.responses.create(
            model=model or self.model,
            reasoning={"effort": reasoning_effort},
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_context},
                        {
                            "type": "input_image",
                            "image_url": f"data:{content_type};base64,{encoded}",
                            "detail": "original",
                        },
                    ],
                }
            ],
            instructions=instructions,
            safety_identifier=self.safety_identifier,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "chart_analysis",
                    "strict": True,
                    "schema": output_schema,
                }
            },
            store=False,
        )
        record_analysis_usage(self, _openai_usage(response))
        return json.loads(response.output_text)
