"""Explicit dependencies supplied by the terminal composition root per invocation.

No module in this package imports app.cli. Services never depend on terminal UI.
The authorization callback is the existing policy/audit boundary, not a new grant.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from rich.console import Console
from sqlalchemy.orm import Session

from app.config import Settings
from app.services.workspaces import RequestScope


class AuthorizeCommand(Protocol):
    def __call__(
        self,
        name: str,
        arguments: dict,
        *,
        mutating: bool = False,
        deterministic: bool = False,
        assume_yes: bool = False,
    ) -> None: ...


@dataclass(frozen=True)
class CommandRuntime:
    console: Console
    session_factory: Callable[[], Session]
    current_scope: Callable[[Session], RequestScope]
    authorize: AuthorizeCommand
    print_model: Callable[[object], None]
    upgrade_database: Callable[[], None]
    get_settings: Callable[[], Settings]
