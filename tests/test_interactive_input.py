from types import SimpleNamespace
from unittest.mock import Mock

from app.clipboard import ClipboardImage, ClipboardImageNotFoundError
from app.interactive_input import IMAGE_MARKER, ClipboardChatPrompt

PNG = b"\x89PNG\r\n\x1a\nchart"


class FakeBuffer:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def insert_text(self, value: str) -> None:
        self.text += value


class PasteSession:
    def __init__(self, *, returned_text: str | None = None) -> None:
        self.returned_text = returned_text
        self.toolbar = None

    def prompt(self, _message, *, key_bindings, bottom_toolbar, style) -> str:
        del style
        buffer = FakeBuffer("Review this setup")
        event = SimpleNamespace(
            current_buffer=buffer,
            app=SimpleNamespace(invalidate=Mock()),
        )
        key_bindings.bindings[0].handler(event)
        self.toolbar = bottom_toolbar()
        return self.returned_text if self.returned_text is not None else buffer.text


def test_ctrl_v_attaches_clipboard_image_and_inserts_visible_chip() -> None:
    image = ClipboardImage(PNG, "image/png", "macOS clipboard")
    session = PasteSession()

    result = ClipboardChatPrompt(
        clipboard_reader=Mock(return_value=image),
        session=session,
    ).read()

    assert result.clipboard_image is image
    assert IMAGE_MARKER in result.text
    assert "Review this setup" in result.text
    assert "attached" in session.toolbar[0][1]


def test_deleting_image_chip_removes_attachment_before_submit() -> None:
    image = ClipboardImage(PNG, "image/png", "macOS clipboard")

    result = ClipboardChatPrompt(
        clipboard_reader=Mock(return_value=image),
        session=PasteSession(returned_text="Review this setup"),
    ).read()

    assert result.clipboard_image is None


def test_clipboard_error_is_shown_without_attaching_an_image() -> None:
    session = PasteSession(returned_text="Continue without an image")

    result = ClipboardChatPrompt(
        clipboard_reader=Mock(
            side_effect=ClipboardImageNotFoundError("No copied image was found.")
        ),
        session=session,
    ).read()

    assert result.clipboard_image is None
    assert "No copied image was found" in session.toolbar[0][1]
