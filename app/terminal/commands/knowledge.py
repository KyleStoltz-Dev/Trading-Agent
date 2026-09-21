"""Knowledge command implementations; registration stays in app.cli."""

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer

from app.services.knowledge_import import import_knowledge_path, import_knowledge_text
from app.services.strategy_workspace import (
    resolve_strategy_version,
    search_strategy_knowledge,
    set_strategy_knowledge_excluded,
)
from app.terminal.runtime import CommandRuntime


def knowledge_import_command(
    runtime: CommandRuntime,
    path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            help="TXT, Markdown, JSON, JSONL, CSV, JS, Discord ZIP, or directory.",
        ),
    ],
    strategy: Annotated[
        str,
        typer.Option(help="Exact isolated strategy receiving this material."),
    ],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Import and index external trading material without changing model weights."""
    runtime.authorize(
        "import_strategy_knowledge",
        {"path": str(path.resolve()), "strategy": strategy},
        mutating=True,
        assume_yes=yes,
    )
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            result = import_knowledge_path(
                db,
                path,
                strategy,
                scope=runtime.current_scope(db),
            )
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            LookupError,
            json.JSONDecodeError,
        ) as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        runtime.print_model(result)


def knowledge_paste_command(
    runtime: CommandRuntime,
    strategy: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()] = "pasted-notes",
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Paste one note interactively into an isolated strategy."""
    text = typer.prompt("Knowledge text")
    runtime.authorize(
        "import_strategy_knowledge",
        {"source": "paste", "strategy": strategy, "name": name},
        mutating=True,
        assume_yes=yes,
    )
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            runtime.print_model(
                import_knowledge_text(
                    db,
                    text,
                    strategy,
                    name,
                    scope=runtime.current_scope(db),
                )
            )
        except (ValueError, LookupError) as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc


def knowledge_search_command(
    runtime: CommandRuntime,
    strategy: Annotated[str, typer.Option()],
    query: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option(min=1, max=25)] = 8,
) -> None:
    """Search only one strategy version's indexed knowledge."""
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        scope = runtime.current_scope(db)
        try:
            playbook, version = resolve_strategy_version(
                db,
                strategy,
                scope=scope,
            )
            items = search_strategy_knowledge(
                db,
                version.id,
                query,
                limit,
                scope=scope,
            )
        except (ValueError, LookupError) as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        runtime.print_model(
            {
                "strategy": playbook.name,
                "version": version.version,
                "results": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "source_reference": item.source_reference,
                        "occurred_at": item.occurred_at,
                        "content": item.content,
                        "content_hash": item.content_hash,
                    }
                    for item in items
                ],
            }
        )


def _set_knowledge_excluded(
    runtime: CommandRuntime,
    item_id: uuid.UUID,
    strategy: str,
    *,
    excluded: bool,
    yes: bool,
) -> None:
    action = "exclude_strategy_knowledge" if excluded else "restore_strategy_knowledge"
    runtime.authorize(
        action,
        {"item_id": str(item_id), "strategy": strategy},
        mutating=True,
        assume_yes=yes,
    )
    runtime.upgrade_database()
    with runtime.session_factory() as db:
        try:
            item = set_strategy_knowledge_excluded(
                db,
                strategy,
                item_id,
                scope=runtime.current_scope(db),
                excluded=excluded,
            )
        except LookupError as exc:
            runtime.console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    status = "excluded from retrieval" if excluded else "restored to retrieval"
    runtime.console.print(f"[green]{item.id} is {status} for {strategy}.[/green]")


def knowledge_exclude_command(
    runtime: CommandRuntime,
    item_id: Annotated[uuid.UUID, typer.Argument(help="Knowledge item UUID.")],
    strategy: Annotated[str, typer.Option(help="Exact strategy version scope.")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Quarantine one item from strategy retrieval without deleting evidence."""
    _set_knowledge_excluded(runtime, item_id, strategy, excluded=True, yes=yes)


def knowledge_restore_command(
    runtime: CommandRuntime,
    item_id: Annotated[uuid.UUID, typer.Argument(help="Knowledge item UUID.")],
    strategy: Annotated[str, typer.Option(help="Exact strategy version scope.")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Restore one quarantined item to its exact strategy version."""
    _set_knowledge_excluded(runtime, item_id, strategy, excluded=False, yes=yes)
