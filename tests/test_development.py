import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.policy import PolicyViolation
from app.services.development import (
    DevelopmentService,
    detect_development_intent,
    development_request,
)


def test_development_intent_requires_a_software_action_and_target() -> None:
    assert detect_development_intent("Change the agent so it shows my current mode.")
    assert detect_development_intent("/develop add a journal shortcut")
    assert not detect_development_intent("I need to change how I enter this setup.")
    assert not detect_development_intent("The market changed its behavior.")
    assert not detect_development_intent("/developer documentation")
    assert development_request("/develop add a journal shortcut") == "add a journal shortcut"


def test_development_rejects_autonomous_order_execution_before_starting() -> None:
    service = DevelopmentService(Settings())

    with pytest.raises(PolicyViolation, match="broker order execution"):
        service.start("Add code that places broker orders after confirmation.")


def test_development_runner_uses_isolated_worktree_and_secret_free_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "AGENTS.md").write_text("# Rules")
    settings = Settings(
        development_repository=repository,
        development_state_directory=Path(".data/development"),
    )
    service = DevelopmentService(settings)
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OANDA_API_TOKEN", "must-not-leak")
    monkeypatch.setattr(
        "app.services.development.shutil.which",
        lambda name: f"/test/bin/{name}",
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("env", {})))
        if command[0].endswith("/codex"):
            return subprocess.CompletedProcess(command, 0, "Implemented and tested.", "")
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, " M app/example.py\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.services.development.subprocess.run", fake_run)

    session = service.start("Add a visible model route label to the CLI.")

    assert session.status == "needs_review"
    assert session.branch.startswith("agent/dev-add-a-visible-model-route-label")
    codex_call = next(call for call in calls if call[0][0].endswith("/codex"))
    assert "--sandbox" in codex_call[0]
    assert "workspace-write" in codex_call[0]
    assert "--ephemeral" in codex_call[0]
    assert "OPENAI_API_KEY" not in codex_call[1]
    assert "OANDA_API_TOKEN" not in codex_call[1]
    assert service.get(session.id).request == session.request
