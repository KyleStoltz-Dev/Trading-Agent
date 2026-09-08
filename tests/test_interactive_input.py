from types import SimpleNamespace
from unittest.mock import Mock

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from app.clipboard import ClipboardImage, ClipboardImageNotFoundError
from app.interactive_input import (
    IMAGE_MARKER,
    ClipboardChatPrompt,
    SlashCommandCompleter,
    TerminalMenuOption,
    choose_inline_terminal_option,
    choose_terminal_option,
)

PNG = b"\x89PNG\r\n\x1a\nchart"


class FakeBuffer:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def insert_text(self, value: str) -> None:
        self.text += value


class PasteSession:
    def __init__(self, *, returned_text: str | None = None) -> None:
        self.returned_text = returned_text
        self.completer = None

    def prompt(
        self,
        _message,
        *,
        key_bindings,
        completer,
        complete_while_typing,
        style,
    ) -> str:
        del style
        assert complete_while_typing is (completer is not None)
        self.completer = completer
        buffer = FakeBuffer("Review this setup")
        event = SimpleNamespace(
            current_buffer=buffer,
            app=SimpleNamespace(invalidate=Mock()),
        )
        key_bindings.bindings[0].handler(event)
        return self.returned_text if self.returned_text is not None else buffer.text


def test_ctrl_v_attaches_clipboard_image_and_inserts_visible_chip() -> None:
    image = ClipboardImage(PNG, "image/png", "macOS clipboard")
    session = PasteSession()
    notices = []

    result = ClipboardChatPrompt(
        clipboard_reader=Mock(return_value=image),
        session=session,
        notice=lambda message, error: notices.append((message, error)),
    ).read()

    assert result.clipboard_image is image
    assert IMAGE_MARKER in result.text
    assert "Review this setup" in result.text
    assert notices == [("Image #1 attached · Enter to analyze and save", False)]


def test_deleting_image_chip_removes_attachment_before_submit() -> None:
    image = ClipboardImage(PNG, "image/png", "macOS clipboard")

    result = ClipboardChatPrompt(
        clipboard_reader=Mock(return_value=image),
        session=PasteSession(returned_text="Review this setup"),
        notice=lambda _message, _error: None,
    ).read()

    assert result.clipboard_image is None


def test_clipboard_error_is_shown_without_attaching_an_image() -> None:
    session = PasteSession(returned_text="Continue without an image")
    notices = []

    result = ClipboardChatPrompt(
        clipboard_reader=Mock(
            side_effect=ClipboardImageNotFoundError("No copied image was found.")
        ),
        session=session,
        notice=lambda message, error: notices.append((message, error)),
    ).read()

    assert result.clipboard_image is None
    assert notices == [("No copied image was found.", True)]


def test_slash_completer_lists_commands_dynamic_values_and_typo_matches() -> None:
    options = (
        TerminalMenuOption("/strategy", "/strategy", "choose a strategy"),
        TerminalMenuOption(
            "/strategy use Price Action",
            "/strategy use Price Action",
            "activate saved strategy",
        ),
        TerminalMenuOption("/sources", "/sources", "show sources"),
    )
    completer = SlashCommandCompleter(lambda _entered: options)

    partial = list(
        completer.get_completions(Document("/s"), CompleteEvent(completion_requested=False))
    )
    strategy = list(
        completer.get_completions(
            Document("/strategy use P"),
            CompleteEvent(completion_requested=False),
        )
    )
    typo = list(
        completer.get_completions(
            Document("/straetedy"),
            CompleteEvent(completion_requested=False),
        )
    )

    assert {item.text for item in partial} == {
        "/strategy",
        "/strategy use Price Action",
        "/sources",
    }
    assert [item.text for item in strategy] == ["/strategy use Price Action"]
    assert [item.text for item in typo] == ["/strategy"]


def test_terminal_choice_menu_returns_selected_value(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.interactive_input.Application.run",
        Mock(return_value="openai\0gpt-5.6-terra"),
    )

    selected = choose_terminal_option(
        "Choose model",
        "Select a provider and model.",
        (
            TerminalMenuOption(
                value="openai\0gpt-5.6-terra",
                label="OpenAI · gpt-5.6-terra",
                description="uses your API key",
            ),
        ),
    )

    assert selected == "openai\0gpt-5.6-terra"


def test_inline_choice_menu_keeps_selection_in_the_current_prompt() -> None:
    session = Mock()
    session.prompt.return_value = "openai/gpt-5.6-terra"

    selected = choose_inline_terminal_option(
        "Model ❯ ",
        (
            TerminalMenuOption(
                value="openai\0gpt-5.6-terra",
                label="ChatGPT subscription · gpt-5.6-terra",
                description="uses your signed-in subscription",
            ),
        ),
        session=session,
    )

    assert selected == "openai\0gpt-5.6-terra"
    assert session.prompt.call_args.kwargs["complete_while_typing"] is True
    assert callable(session.prompt.call_args.kwargs["pre_run"])
