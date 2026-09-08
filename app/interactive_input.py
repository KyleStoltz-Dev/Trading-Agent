import platform
from collections.abc import Callable
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.styles import Style

from app.clipboard import ClipboardImage, ClipboardImageError, read_clipboard_image

IMAGE_MARKER = "[Image #1]"


@dataclass(frozen=True)
class ChatPromptResult:
    text: str
    clipboard_image: ClipboardImage | None


@dataclass(frozen=True)
class TerminalMenuOption:
    value: str
    label: str
    description: str = ""


def choose_terminal_option(
    title: str,
    text: str,
    options: tuple[TerminalMenuOption, ...],
) -> str | None:
    """Show an arrow-key terminal selector and return the selected value."""
    if not options:
        return None
    values = [
        (option.value, f"{option.label} — {option.description}".rstrip(" —"))
        for option in options
    ]
    return radiolist_dialog(
        title=title,
        text=text,
        values=values,
        ok_text="Use selection",
        cancel_text="Cancel",
    ).run()


class ClipboardChatPrompt:
    """Interactive prompt with a Claude-style clipboard-image attachment shortcut."""

    def __init__(
        self,
        *,
        clipboard_reader: Callable[[], ClipboardImage] = read_clipboard_image,
        session: PromptSession | None = None,
    ) -> None:
        self._clipboard_reader = clipboard_reader
        self._session = session or PromptSession()
        self._style = Style.from_dict(
            {
                "user": "bold ansicyan",
                "toolbar": "bg:#303030 #dddddd",
                "toolbar-error": "bg:#303030 #ff6b6b",
                "toolbar-ready": "bg:#303030 #7ee787",
            }
        )

    def read(self) -> ChatPromptResult:
        attachment: ClipboardImage | None = None
        status = "Ctrl-V: attach a copied screenshot"
        status_style = "class:toolbar"
        bindings = KeyBindings()

        def attach(event) -> None:
            nonlocal attachment, status, status_style
            try:
                attachment = self._clipboard_reader()
            except ClipboardImageError as exc:
                attachment = None
                status = str(exc)
                status_style = "class:toolbar-error"
            else:
                if IMAGE_MARKER not in event.current_buffer.text:
                    prefix = "" if not event.current_buffer.text else " "
                    event.current_buffer.insert_text(f"{prefix}{IMAGE_MARKER} ")
                status = "Image #1 attached · add context and press Enter"
                status_style = "class:toolbar-ready"
            event.app.invalidate()

        bindings.add("c-v")(attach)
        if platform.system().lower() == "windows":
            bindings.add("escape", "v")(attach)

        def toolbar() -> FormattedText:
            return FormattedText([(status_style, f" {status} ")])

        text = self._session.prompt(
            FormattedText(
                [
                    ("class:user", "You"),
                    ("", " ❯ "),
                ]
            ),
            key_bindings=bindings,
            bottom_toolbar=toolbar,
            style=self._style,
        ).strip()
        retained_attachment = attachment if IMAGE_MARKER in text else None
        return ChatPromptResult(text=text, clipboard_image=retained_attachment)
