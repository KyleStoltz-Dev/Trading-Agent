"""Boundary and behavior contracts for the first terminal decomposition."""

import ast
import inspect
import uuid
from contextlib import nullcontext
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer
from rich.console import Console
from typer.main import get_command
from typer.testing import CliRunner

from app import cli
from app.config import Settings
from app.policy import PolicyViolation
from app.terminal.commands import experiments, knowledge, news, sessions
from app.terminal.runtime import CommandRuntime

GROUPS = {
    "news": (news, ["news_sync", "news_upcoming", "news_history", "news_watch"]),
    "knowledge": (knowledge, [
        "knowledge_import_command", "knowledge_paste_command", "knowledge_search_command",
        "knowledge_exclude_command", "knowledge_restore_command",
    ]),
    "experiment": (experiments, [
        "experiment_start", "experiment_sample", "experiment_correlations",
        "experiment_report", "experiment_complete", "experiment_show",
    ]),
    "sessions": (sessions, ["sessions_list", "sessions_show"]),
}
COMMANDS = [(module, name) for module, names in GROUPS.values() for name in names]


@pytest.fixture
def runtime():
    return CommandRuntime(
        console=Console(file=StringIO(), force_terminal=False),
        session_factory=Mock(), current_scope=Mock(), authorize=Mock(),
        print_model=Mock(), upgrade_database=Mock(),
        get_settings=Mock(return_value=Settings()),
    )


@pytest.mark.parametrize("module,name", COMMANDS, ids=[name for _, name in COMMANDS])
def test_cli_facade_keeps_signature_and_forwards_explicit_invocation(
    monkeypatch, runtime, module, name,
):
    facade = getattr(cli, name)
    implementation = getattr(module, name)
    signature = inspect.signature(facade)
    implementation_signature = inspect.signature(implementation)
    assert list(implementation_signature.parameters)[0] == "runtime"
    # Typer OptionInfo instances differ by identity; compare their declaration syntax.
    facade_arguments = ast.parse(inspect.getsource(facade)).body[0].args
    implementation_arguments = ast.parse(inspect.getsource(implementation)).body[0].args
    implementation_arguments.args.pop(0)
    assert ast.dump(facade_arguments) == ast.dump(implementation_arguments)
    # Distinct sentinel values prove every argument is forwarded without substitution.
    arguments = {parameter: object() for parameter in signature.parameters}
    handler = Mock()
    monkeypatch.setattr(module, name, handler)
    monkeypatch.setattr(cli, "_command_runtime", lambda: runtime)
    facade(**arguments)
    handler.assert_called_once_with(runtime, **arguments)


@pytest.mark.parametrize("group", GROUPS)
def test_command_names_and_help_remain_available_without_runtime_access(monkeypatch, group):
    factory = Mock(side_effect=AssertionError("help must not initialize runtime"))
    monkeypatch.setattr(cli, "_command_runtime", factory)
    registered = get_command(cli.app).commands[group]
    expected = {
        "news": {"sync", "upcoming", "history", "watch"},
        "knowledge": {"import", "paste", "search", "exclude", "restore"},
        "experiment": {"start", "sample", "correlations", "report", "complete", "show"},
        "sessions": {"list", "show"},
    }
    assert set(registered.commands) == expected[group]
    for command in registered.commands:
        result = CliRunner().invoke(cli.app, [group, command, "--help"])
        assert result.exit_code == 0, result.output
        assert "--runtime" not in result.output
    factory.assert_not_called()


@pytest.mark.parametrize("action", ["import", "exclude", "restore", "experiment", "watch"])
def test_policy_failure_prevents_extracted_command_side_effects(runtime, monkeypatch, action):
    runtime.authorize.side_effect = PolicyViolation("blocked by policy")
    monkeypatch.setattr(news, "news_provider_configured", lambda _: True)
    operations = {
        "import": lambda: knowledge.knowledge_import_command(
            runtime, path=Path("example.txt"), strategy="gold", yes=True,
        ),
        "exclude": lambda: knowledge.knowledge_exclude_command(
            runtime, item_id=uuid.uuid4(), strategy="gold", yes=True,
        ),
        "restore": lambda: knowledge.knowledge_restore_command(
            runtime, item_id=uuid.uuid4(), strategy="gold", yes=True,
        ),
        "experiment": lambda: experiments.experiment_complete(runtime, "test", yes=True),
        "watch": lambda: news.news_watch(runtime, once=True, yes=True),
    }
    with pytest.raises(PolicyViolation):
        operations[action]()
    runtime.authorize.assert_called_once()
    assert runtime.authorize.call_args.kwargs["mutating"] is True
    # --yes only skips the human prompt; it never bypasses policy authorization.
    assert runtime.authorize.call_args.kwargs["assume_yes"] is True
    runtime.upgrade_database.assert_not_called()
    runtime.session_factory.assert_not_called()


def test_session_transcript_preserves_scope_and_limit(runtime, monkeypatch):
    database, scope = object(), object()
    conversation = SimpleNamespace(id=uuid.uuid4())
    runtime.session_factory.return_value = nullcontext(database)
    runtime.current_scope.return_value = scope
    lookup = Mock(return_value=conversation)
    transcript = Mock(return_value=[{"role": "user", "content": "Review gold"}])
    monkeypatch.setattr(sessions, "resolve_conversation", lookup)
    monkeypatch.setattr(sessions, "conversation_transcript", transcript)
    sessions.sessions_show(runtime, "gold")
    lookup.assert_called_once_with(database, "gold", scope=scope)
    transcript.assert_called_once_with(database, conversation, scope=scope, limit=100)
    assert "Review gold" in runtime.console.file.getvalue()


def test_news_outage_closes_connector_and_keeps_stored_data(runtime, monkeypatch):
    from unittest.mock import AsyncMock

    connector = SimpleNamespace(
        calendar=AsyncMock(side_effect=RuntimeError("rate limited")),
        news=AsyncMock(), aclose=AsyncMock(),
    )
    monkeypatch.setattr(news, "create_news_connector", lambda _: connector)
    with pytest.raises(typer.Exit) as error:
        news.news_sync(runtime, start="2026-09-20", end="2026-09-21", yes=True)
    assert error.value.exit_code == 1
    connector.aclose.assert_awaited_once()
    runtime.session_factory.assert_not_called()
    assert "Previously stored calendar data remains available" in runtime.console.file.getvalue()


def test_each_invocation_uses_its_own_console(runtime, monkeypatch):
    second_output = StringIO()
    second = replace(runtime, console=Console(file=second_output))
    monkeypatch.setattr(news, "news_provider_configured", lambda _: False)
    for invocation in (runtime, second):
        with pytest.raises(typer.Exit) as error:
            news.news_watch(invocation, once=True)
        assert error.value.exit_code == 2
    assert runtime.console.file.getvalue().count("Select a configured") == 1
    assert second_output.getvalue().count("Select a configured") == 1


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""
            if node.module == "app":
                yield from ("app." + alias.name for alias in node.names)


def test_shared_services_and_connectors_never_import_interface_entrypoints_or_model_sdks():
    root = Path(__file__).resolve().parents[1] / "app"
    forbidden = ("app.cli", "app.main", "app.terminal", "openai", "anthropic")
    violations = []
    for package in ("services", "connectors"):
        for path in (root / package).rglob("*.py"):
            for module in _imports(path):
                if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                    violations.append(f"{path.relative_to(root)} imports {module}")
    assert not violations, "\n".join(violations)


def test_terminal_modules_do_not_import_cli_or_api_backwards():
    root = Path(__file__).resolve().parents[1] / "app" / "terminal"
    for path in root.rglob("*.py"):
        assert not {"app.cli", "app.main"}.intersection(_imports(path)), path


def test_formatting_has_no_application_dependencies():
    root = Path(__file__).resolve().parents[1]
    assert set(_imports(root / "app/terminal/formatting.py")) == {"re", "unicodedata"}
