import base64
import json
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.providers.base import ProviderConfigurationError, ToolExecutor, safe_tool_error


def _validate_base_url(settings: Settings) -> str:
    parsed = urlparse(settings.ollama_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError("OLLAMA_BASE_URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderConfigurationError(
            "OLLAMA_BASE_URL cannot contain credentials, a query, or a fragment"
        )
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not settings.ollama_allow_remote:
        raise ProviderConfigurationError(
            "Remote Ollama requires OLLAMA_ALLOW_REMOTE=true because prompts leave this device"
        )
    return settings.ollama_base_url.rstrip("/")


def _ollama_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in tools
    ]


def _thinking_level(reasoning_effort: str) -> str:
    return reasoning_effort if reasoning_effort in {"low", "medium", "high"} else "medium"


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.model = settings.ollama_model
        self.context_length = settings.ollama_context_length
        self.keep_alive = settings.ollama_keep_alive
        self.base_url = _validate_base_url(settings)
        self.client = client or httpx.Client(
            base_url=self.base_url,
            timeout=settings.ollama_request_timeout_seconds,
            trust_env=False,
        )

    def installed_models(self) -> frozenset[str]:
        try:
            response = self.client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderConfigurationError(f"Ollama is unavailable at {self.base_url}") from exc
        if not isinstance(data, dict):
            raise ProviderConfigurationError("Ollama returned an invalid model list")
        return frozenset(
            item["name"]
            for item in data.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )

    def smoke_test(self) -> str:
        data = self._chat(
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Reply with exactly READY.",
                    }
                ],
                "stream": False,
                "think": False,
                "keep_alive": self.keep_alive,
                "options": {
                    "num_ctx": min(self.context_length, 2048),
                    "num_predict": 8,
                    "temperature": 0,
                },
            }
        )
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Ollama model did not generate a smoke-test response")
        return content.strip()

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
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": instructions},
            *history,
            {"role": "user", "content": message},
        ]
        provider_tools = _ollama_tools(tools)
        for _ in range(max_tool_rounds):
            data = self._chat(
                {
                    "model": model or self.model,
                    "messages": messages,
                    "tools": provider_tools,
                    "stream": False,
                    "think": _thinking_level(reasoning_effort),
                    "keep_alive": self.keep_alive,
                    "options": {"num_ctx": self.context_length},
                }
            )
            assistant = data.get("message") or {}
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                content = assistant.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("Ollama returned neither text nor tool calls")
                return content

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name")
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = None
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    output = json.dumps({"ok": False, "error": "invalid Ollama tool call"})
                    name = name if isinstance(name, str) else "unknown_tool"
                else:
                    try:
                        output = execute_tool(name, arguments)
                    except Exception as exc:
                        output = json.dumps({"ok": False, "error": safe_tool_error(exc)})
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": output,
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
        model: str | None = None,
        reasoning_effort: str = "medium",
    ) -> dict[str, Any]:
        del content_type
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data = self._chat(
            {
                "model": model or self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {
                        "role": "user",
                        "content": user_context,
                        "images": [encoded],
                    },
                ],
                "format": output_schema,
                "stream": False,
                "think": _thinking_level(reasoning_effort),
                "keep_alive": self.keep_alive,
                "options": {
                    "num_ctx": self.context_length,
                    "temperature": 0,
                },
            }
        )
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("Ollama did not return structured chart analysis")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned invalid structured chart analysis") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Ollama returned invalid structured chart analysis")
        return result

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Ollama request failed with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderConfigurationError(f"Ollama is unavailable at {self.base_url}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Ollama returned an invalid response")
        return data
