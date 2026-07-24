from collections.abc import Callable
from typing import Any, Protocol

ToolExecutor = Callable[[str, dict[str, Any]], str]


class ProviderConfigurationError(RuntimeError):
    pass


def safe_tool_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, LookupError, ProviderConfigurationError)):
        return str(exc)[:500]
    return f"{type(exc).__name__}: tool execution failed"


class ModelProvider(Protocol):
    name: str
    model: str

    def complete(
        self,
        *,
        instructions: str,
        message: str,
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        execute_tool: ToolExecutor,
        max_tool_rounds: int,
    ) -> str: ...

    def analyze_chart(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        user_context: str,
        instructions: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]: ...
