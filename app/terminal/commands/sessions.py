"""Sessions command implementations; registration stays in app.cli."""

from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from app.models import (
    ConversationSession,
)
from app.services.conversations import (
    conversation_transcript,
    list_conversations,
    resolve_conversation,
)
from app.terminal.runtime import CommandRuntime


def sessions_list(
    runtime: CommandRuntime,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    show_internal_ids: Annotated[bool, typer.Option()] = False,
) -> None:
    """List recent interactive sessions."""
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        conversations = list_conversations(db, limit, scope=runtime.current_scope(db))
        table = Table(title="Trading Agent sessions")
        table.add_column("Name")
        table.add_column("Title")
        table.add_column("Updated")
        if show_internal_ids:
            table.add_column("Internal UUID")
        for conversation in conversations:
            values = [
                conversation.name,
                conversation.title,
                str(conversation.updated_at),
            ]
            if show_internal_ids:
                values.append(str(conversation.id))
            table.add_row(*values)
        runtime.console.print(table)


def sessions_show(runtime: CommandRuntime, session: str) -> None:
    """Show the saved transcript for one session."""
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        scope = runtime.current_scope(db)
        conversation: ConversationSession | None = resolve_conversation(
            db,
            session,
            scope=scope,
        )
        if conversation is None:
            runtime.console.print(f"[red]Conversation {session} was not found.[/red]")
            raise typer.Exit(1)
        for turn in conversation_transcript(
            db,
            conversation,
            scope=scope,
            limit=100,
        ):
            runtime.console.print(Panel(turn["content"], title=turn["role"]))
