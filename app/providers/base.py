import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, Protocol

from app.costs import TokenUsage

ToolExecutor = Callable[[str, dict[str, Any]], str]
_CAPACITY_LOCK = threading.Lock()
_CAPACITY_LIMITERS: dict[tuple[str, int], threading.BoundedSemaphore] = {}


def provider_capacity_limiter(
    provider: str,
    maximum: int,
) -> threading.BoundedSemaphore:
    key = (provider, maximum)
    with _CAPACITY_LOCK:
        limiter = _CAPACITY_LIMITERS.get(key)
        if limiter is None:
            limiter = threading.BoundedSemaphore(maximum)
            _CAPACITY_LIMITERS[key] = limiter
        return limiter


def limit_provider_capacity(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        acquired = self._capacity_limiter.acquire(
            timeout=self._capacity_queue_timeout_seconds
        )
        if not acquired:
            raise RuntimeError(
                f"{self.name} model request capacity is full; retry after an active "
                "analysis finishes"
            )
        try:
            return method(self, *args, **kwargs)
        finally:
            self._capacity_limiter.release()

    return wrapper


def track_completion_usage(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        self.last_usage = TokenUsage()
        self._completion_usage_active = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self._completion_usage_active = False

    return wrapper


def record_analysis_usage(provider: Any, usage: TokenUsage) -> None:
    if getattr(provider, "_completion_usage_active", False):
        provider.last_usage += usage
    else:
        provider.last_usage = usage


class ProviderConfigurationError(RuntimeError):
    pass


def safe_tool_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, LookupError, ProviderConfigurationError)):
        return str(exc)[:500]
    return f"{type(exc).__name__}: tool execution failed"


class ModelProvider(Protocol):
    name: str
    model: str
    last_usage: TokenUsage

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
    ) -> str: ...

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
    ) -> dict[str, Any]: ...
