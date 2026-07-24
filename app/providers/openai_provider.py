import base64
import importlib
import json
from typing import Any

from app.config import Settings, secret_value
from app.providers.base import ProviderConfigurationError, ToolExecutor, safe_tool_error


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.openai_model
        self.safety_identifier = settings.openai_safety_identifier
        if client is None:
            try:
                openai = importlib.import_module("openai")
            except ImportError as exc:
                raise ProviderConfigurationError(
                    'Install the OpenAI adapter with `pip install -e ".[openai]"`'
                ) from exc
            client = openai.OpenAI(api_key=secret_value(settings.openai_api_key))
        self.client = client

    def complete(
        self,
        *,
        instructions: str,
        message: str,
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        execute_tool: ToolExecutor,
        max_tool_rounds: int,
    ) -> str:
        input_items: list[Any] = [*history, {"role": "user", "content": message}]
        for _ in range(max_tool_rounds):
            response = self.client.responses.create(
                model=self.model,
                instructions=instructions,
                input=input_items,
                tools=tools,
                reasoning={"effort": "medium", "context": "current_turn"},
                safety_identifier=self.safety_identifier,
                store=False,
            )
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

    def analyze_chart(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        user_context: str,
        instructions: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": "medium"},
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
        )
        return json.loads(response.output_text)
