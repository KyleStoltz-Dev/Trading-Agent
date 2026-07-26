import base64
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from filelock import FileLock, Timeout

from app.config import Settings
from app.costs import TokenUsage
from app.providers.base import (
    ProviderConfigurationError,
    ToolExecutor,
    record_analysis_usage,
    safe_tool_error,
    track_completion_usage,
)
from app.system_resources import ModelFitAssessment, assess_model_fit, resource_snapshot


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


def _ollama_usage(data: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=int(data.get("prompt_eval_count") or 0),
        output_tokens=int(data.get("eval_count") or 0),
    )


def _ollama_performance(data: dict[str, Any]) -> dict[str, float]:
    def seconds(key: str) -> float:
        return float(data.get(key) or 0) / 1_000_000_000

    output_seconds = seconds("eval_duration")
    prompt_seconds = seconds("prompt_eval_duration")
    return {
        "total_seconds": round(seconds("total_duration"), 3),
        "load_seconds": round(seconds("load_duration"), 3),
        "prompt_tokens_per_second": round(
            int(data.get("prompt_eval_count") or 0) / prompt_seconds,
            2,
        )
        if prompt_seconds
        else 0,
        "output_tokens_per_second": round(
            int(data.get("eval_count") or 0) / output_seconds,
            2,
        )
        if output_seconds
        else 0,
    }


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.model = settings.ollama_model
        self.last_usage = TokenUsage()
        self.last_performance: dict[str, float] = {}
        self.context_length = settings.ollama_context_length
        self.keep_alive = settings.ollama_keep_alive
        self.base_url = _validate_base_url(settings)
        hostname = (urlparse(self.base_url).hostname or "").lower()
        self.local_runtime = hostname in {"127.0.0.1", "::1", "localhost"}
        self.last_resource_assessment: ModelFitAssessment | None = None
        self.client = client or httpx.Client(
            base_url=self.base_url,
            timeout=settings.ollama_request_timeout_seconds,
            trust_env=False,
        )

    def _installed_model_records(self) -> list[dict[str, Any]]:
        try:
            response = self.client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderConfigurationError(f"Ollama is unavailable at {self.base_url}") from exc
        if not isinstance(data, dict):
            raise ProviderConfigurationError("Ollama returned an invalid model list")
        records = data.get("models", [])
        if not isinstance(records, list):
            raise ProviderConfigurationError("Ollama returned an invalid model list")
        return [
            item
            for item in records
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]

    def installed_models(self) -> frozenset[str]:
        return frozenset(item["name"] for item in self._installed_model_records())

    def installed_model_sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for item in self._installed_model_records():
            size = item.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                sizes[item["name"]] = size
            else:
                sizes[item["name"]] = 0
        return sizes

    def loaded_model_records(self) -> list[dict[str, Any]]:
        try:
            response = self.client.get("/api/ps")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderConfigurationError(
                f"Ollama is unavailable at {self.base_url}"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderConfigurationError("Ollama returned an invalid process list")
        records = data.get("models", [])
        if not isinstance(records, list):
            raise ProviderConfigurationError("Ollama returned an invalid process list")
        return [
            item
            for item in records
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]

    def loaded_models(self) -> frozenset[str]:
        return frozenset(item["name"] for item in self.loaded_model_records())

    def _runtime_lock(self):
        if not self.local_runtime:
            return nullcontext()
        lock_path = Path(self.settings.ollama_runtime_lock_path).expanduser().resolve()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ProviderConfigurationError(
                f"Cannot create Ollama runtime lock directory: {lock_path.parent}"
            ) from exc
        return FileLock(
            str(lock_path),
            timeout=self.settings.ollama_runtime_lock_timeout_seconds,
        )

    def _preflight_model(self, model: str) -> None:
        self.last_resource_assessment = None
        if not self.local_runtime or not self.settings.resource_aware_model_routing:
            return
        model_sizes = self.installed_model_sizes()
        model_size = model_sizes.get(model, 0)
        if model_size <= 0:
            raise ProviderConfigurationError(
                f"Cannot safely estimate local model {model}; its installed size is unknown"
            )
        loaded = self.loaded_models()
        assessment = assess_model_fit(
            model=model,
            model_size_bytes=model_size,
            context_length=self.context_length,
            memory_reserve_gb=self.settings.model_memory_reserve_gb,
            memory_block_percent=self.settings.model_memory_block_percent,
            swap_block_percent=self.settings.model_swap_block_percent,
            currently_loaded=model in loaded,
            snapshot=resource_snapshot(),
        )
        self.last_resource_assessment = assessment
        if assessment.status == "block":
            raise RuntimeError(
                f"Local model {model} blocked by the resource guard: "
                f"{assessment.reason}"
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
                "keep_alive": 0 if self.local_runtime else self.keep_alive,
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

    def unload_model(self, model: str) -> None:
        if not self.local_runtime and not self.settings.ollama_manage_remote_runtime:
            raise ProviderConfigurationError(
                "Remote Ollama model unloading requires "
                "OLLAMA_MANAGE_REMOTE_RUNTIME=true"
            )
        try:
            response = self.client.post(
                "/api/generate",
                json={"model": model, "stream": False, "keep_alive": 0},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderConfigurationError(
                f"Ollama could not unload {model}"
            ) from exc

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
            self.last_usage += _ollama_usage(data)
            self.last_performance = _ollama_performance(data)
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
        record_analysis_usage(self, _ollama_usage(data))
        self.last_performance = _ollama_performance(data)
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
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            raise RuntimeError("Ollama request is missing a model")
        try:
            with self._runtime_lock():
                self._preflight_model(model)
                response = self.client.post("/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except Timeout as exc:
            raise RuntimeError(
                "Another Trading Agent process is using the local Ollama runtime; "
                "retry after it finishes"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Ollama request failed with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderConfigurationError(f"Ollama is unavailable at {self.base_url}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Ollama returned an invalid response")
        return data
