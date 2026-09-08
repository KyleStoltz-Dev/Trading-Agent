import platform
from collections.abc import Callable
from dataclasses import dataclass
from difflib import get_close_matches

from prompt_toolkit import Application, PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Dialog, Label, RadioList

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


class InlineMenuCompleter(Completer):
    """Render a compact completion list without taking over the terminal."""

    def __init__(self, options: tuple[TerminalMenuOption, ...]) -> None:
        self._options = options

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ):
        del complete_event
        entered = document.text_before_cursor
        normalized = entered.casefold()
        for option in self._options:
            input_value = option.value.replace("\0", "/")
            if (
                normalized
                and normalized not in input_value.casefold()
                and normalized not in option.label.casefold()
            ):
                continue
            yield Completion(
                input_value,
                start_position=-len(entered),
                display=option.label,
                display_meta=option.description,
            )


def choose_inline_terminal_option(
    prompt_text: str,
    options: tuple[TerminalMenuOption, ...],
    *,
    session: PromptSession | None = None,
) -> str | None:
    """Choose from an inline completion menu and return the option's internal value."""
    if not options:
        return None
    active_session = session or PromptSession()
    style = Style.from_dict(
        {
            "choice": "bold ansicyan",
            "completion-menu.completion": "bg:#1f2937 #d1d5db",
            "completion-menu.completion.current": "bg:#374151 #38bdf8 bold",
            "completion-menu.meta.completion": "bg:#1f2937 #9ca3af",
            "completion-menu.meta.completion.current": "bg:#374151 #d1d5db",
        }
    )
    try:
        def show_choices() -> None:
            active_session.default_buffer.start_completion(select_first=False)

        answer = active_session.prompt(
            FormattedText([("class:choice", prompt_text)]),
            completer=InlineMenuCompleter(options),
            complete_while_typing=True,
            pre_run=show_choices,
            style=style,
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not answer:
        return None
    by_input = {option.value.replace("\0", "/").casefold(): option.value for option in options}
    by_label = {option.label.casefold(): option.value for option in options}
    return by_input.get(answer.casefold()) or by_label.get(answer.casefold())


class SlashCommandCompleter(Completer):
    """Complete discoverable slash commands and their dynamic choices."""

    def __init__(
        self,
        options: Callable[[str], tuple[TerminalMenuOption, ...]],
    ) -> None:
        self._options = options

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ):
        del complete_event
        entered = document.text_before_cursor
        if not entered.startswith("/"):
            return
        options = self._options(entered)
        matches = [
            option for option in options if option.value.casefold().startswith(entered.casefold())
        ]
        if not matches and " " not in entered:
            roots = {
                option.value.casefold(): option
                for option in options
                if " " not in option.value
            }
            matches = [
                roots[value]
                for value in get_close_matches(
                    entered.casefold(),
                    roots,
                    n=5,
                    cutoff=0.55,
                )
            ]
        seen: set[str] = set()
        for option in matches:
            if option.value in seen:
                continue
            seen.add(option.value)
            yield Completion(
                option.value,
                start_position=-len(entered),
                display=option.value,
                display_meta=option.description,
            )


def choose_terminal_option(
    title: str,
    text: str,
    options: tuple[TerminalMenuOption, ...],
    *,
    show_descriptions: bool = True,
    default: str | None = None,
) -> str | None:
    """Show an arrow-key selector where Enter immediately applies the choice."""
    if not options:
        return None
    values = [
        (
            option.value,
            (
                f"{option.label} — {option.description}"
                if show_descriptions and option.description
                else option.label
            ),
        )
        for option in options
    ]
    choices = RadioList(
        values=values,
        default=default or values[0][0],
        select_on_focus=True,
        show_numbers=True,
    )
    bindings = KeyBindings()

    @bindings.add("enter", eager=True)
    def accept(event) -> None:
        event.app.exit(result=choices.current_value)

    @bindings.add("escape", eager=True)
    @bindings.add("c-c", eager=True)
    def cancel(event) -> None:
        event.app.exit(result=None)

    body = HSplit(
        [
            Label(text=text, dont_extend_height=True, wrap_lines=True),
            choices,
            Label(
                text="↑/↓ move · Enter use · Esc cancel",
                style="class:menu-help",
                dont_extend_height=True,
            ),
        ],
        padding=1,
    )
    dialog = Dialog(
        title=title,
        body=body,
        buttons=[],
        with_background=True,
    )
    style = Style.from_dict(
        {
            "dialog": "bg:#1f2937",
            "dialog.body": "bg:#1f2937 #e5e7eb",
            "dialog frame.label": "bg:#1f2937 #38bdf8 bold",
            "radio": "#d1d5db",
            "radio-selected": "bg:#374151 #38bdf8 bold",
            "radio-checked": "#38bdf8 bold",
            "radio-number": "#9ca3af",
            "menu-help": "#9ca3af",
        }
    )
    return Application(
        layout=Layout(dialog, focused_element=choices),
        key_bindings=bindings,
        style=style,
        mouse_support=True,
        full_screen=True,
    ).run()


class ClipboardChatPrompt:
    """Interactive prompt with a Claude-style clipboard-image attachment shortcut."""

    def __init__(
        self,
        *,
        clipboard_reader: Callable[[], ClipboardImage] = read_clipboard_image,
        session: PromptSession | None = None,
        command_options: Callable[[str], tuple[TerminalMenuOption, ...]] | None = None,
        notice: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._clipboard_reader = clipboard_reader
        self._session = session or PromptSession(history=InMemoryHistory())
        self._completer = (
            SlashCommandCompleter(command_options) if command_options is not None else None
        )
        self._notice = notice
        self._style = Style.from_dict(
            {
                "user": "bold ansicyan",
                "notice": "#7ee787",
                "notice-error": "#ff6b6b",
                "completion-menu.completion": "bg:#1f2937 #d1d5db",
                "completion-menu.completion.current": "bg:#374151 #38bdf8 bold",
                "completion-menu.meta.completion": "bg:#1f2937 #9ca3af",
                "completion-menu.meta.completion.current": "bg:#374151 #d1d5db",
            }
        )

    def _show_notice(self, message: str, *, error: bool = False) -> None:
        if self._notice is not None:
            self._notice(message, error)
            return

        def render() -> None:
            style = "class:notice-error" if error else "class:notice"
            print_formatted_text(FormattedText([(style, message)]), style=self._style)

        run_in_terminal(render)

    def read(self) -> ChatPromptResult:
        attachment: ClipboardImage | None = None
        bindings = KeyBindings()

        def attach(event) -> None:
            nonlocal attachment
            try:
                attachment = self._clipboard_reader()
            except ClipboardImageError as exc:
                attachment = None
                self._show_notice(str(exc), error=True)
            else:
                if IMAGE_MARKER not in event.current_buffer.text:
                    prefix = "" if not event.current_buffer.text else " "
                    event.current_buffer.insert_text(f"{prefix}{IMAGE_MARKER} ")
                self._show_notice("Image #1 attached · Enter to analyze and save")
            event.app.invalidate()

        bindings.add("c-v")(attach)
        if platform.system().lower() == "windows":
            bindings.add("escape", "v")(attach)

        @bindings.add("escape", "enter")
        def insert_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        text = self._session.prompt(
            FormattedText(
                [
                    ("class:user", "You"),
                    ("", " ❯ "),
                ]
            ),
            key_bindings=bindings,
            completer=self._completer,
            complete_while_typing=self._completer is not None,
            style=self._style,
        ).strip()
        retained_attachment = attachment if IMAGE_MARKER in text else None
        return ChatPromptResult(text=text, clipboard_image=retained_attachment)
