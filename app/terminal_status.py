from __future__ import annotations

import threading
from types import TracebackType
from typing import Protocol

from rich.console import Console
from rich.markup import escape

THINKING_TIPS = (
    "Press Ctrl-C to cancel this response and stay in the chat.",
    "Press Ctrl-V at the prompt to attach a copied chart.",
    "Ask for sources whenever you want to inspect the evidence used.",
    "Broker facts and chart interpretations remain separate in every review.",
)


class _Status(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def update(self, status: str) -> None: ...


class ThinkingStatus:
    """Render one transient thinking display whose tip changes in place."""

    def __init__(
        self,
        console: Console,
        label: str,
        *,
        tips: tuple[str, ...] = THINKING_TIPS,
        interval_seconds: float = 7.0,
    ) -> None:
        self.console = console
        self.label = label
        self.tips = tips
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._status: _Status | None = None
        self._thread: threading.Thread | None = None

    def _render(self, index: int) -> str:
        if not self.tips:
            return self.label
        tip = escape(self.tips[index % len(self.tips)])
        return f"{self.label}\n[dim]Tip: {tip}[/dim]"

    def _rotate(self) -> None:
        index = 1
        while not self._stop.wait(self.interval_seconds):
            status = self._status
            if status is None:
                return
            status.update(self._render(index))
            index += 1

    def __enter__(self) -> ThinkingStatus:
        self._status = self.console.status(
            self._render(0),
            spinner="dots",
            refresh_per_second=8,
        )
        self._status.start()
        if (
            self.console.is_terminal
            and len(self.tips) > 1
            and self.interval_seconds > 0
        ):
            self._thread = threading.Thread(
                target=self._rotate,
                name="trading-agent-thinking-tips",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(max(self.interval_seconds, 0.05), 0.25))
        if self._status is not None:
            self._status.stop()
