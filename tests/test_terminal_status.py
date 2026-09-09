import time

from app.terminal_status import ThinkingStatus


class FakeStatus:
    def __init__(self, initial: str) -> None:
        self.updates = [initial]
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def update(self, status: str) -> None:
        self.updates.append(status)


class FakeConsole:
    is_terminal = True

    def __init__(self) -> None:
        self.display: FakeStatus | None = None
        self.calls = 0

    def status(self, initial: str, **_kwargs) -> FakeStatus:
        self.calls += 1
        self.display = FakeStatus(initial)
        return self.display


def test_thinking_tips_update_one_transient_display_and_stop_cleanly() -> None:
    console = FakeConsole()

    with ThinkingStatus(
        console,  # type: ignore[arg-type]
        "Thinking",
        tips=("Cancel with Ctrl-C.", "Attach with Ctrl-V."),
        interval_seconds=0.01,
    ):
        time.sleep(0.04)

    assert console.calls == 1
    assert console.display is not None
    assert console.display.started
    assert console.display.stopped
    assert any("Cancel with Ctrl-C" in update for update in console.display.updates)
    assert any("Attach with Ctrl-V" in update for update in console.display.updates)
    update_count = len(console.display.updates)
    time.sleep(0.02)
    assert len(console.display.updates) == update_count


def test_non_terminal_thinking_display_does_not_start_tip_thread() -> None:
    console = FakeConsole()
    console.is_terminal = False

    with ThinkingStatus(
        console,  # type: ignore[arg-type]
        "Thinking",
        tips=("One", "Two"),
        interval_seconds=0.001,
    ):
        time.sleep(0.01)

    assert console.display is not None
    assert len(console.display.updates) == 1
