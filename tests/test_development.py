import os
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


def test_development_rejects_option_like_base_ref_before_git(
    monkeypatch,
) -> None:
    service = DevelopmentService(Settings(development_base_ref="--upload-pack=evil"))
    monkeypatch.setattr(
        service,
        "_verify_repository",
        lambda: pytest.fail("invalid ref must fail before repository commands"),
    )

    with pytest.raises(ValueError, match="base ref"):
        service.start("Add a CLI label.")


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

    private_home = tmp_path / "private-home"
    private_home.mkdir()
    (private_home / ".env").write_text("CANARY=home-secret")
    codex_home = private_home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"token":"test-auth"}')
    monkeypatch.setenv("HOME", str(private_home))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OANDA_API_TOKEN", "must-not-leak")
    monkeypatch.setenv("TRADING_AGENT_SECRET_CANARY", "must-not-leak")
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
    assert "TRADING_AGENT_SECRET_CANARY" not in codex_call[1]
    assert codex_call[1]["HOME"] != str(private_home)
    assert codex_call[1]["XDG_CONFIG_HOME"] != str(private_home)
    assert codex_call[1]["CODEX_HOME"] != str(codex_home)
    assert not Path(codex_call[1]["CODEX_HOME"]).is_relative_to(repository)
    assert codex_call[1]["GIT_CONFIG_GLOBAL"] in {"/dev/null", "NUL"}
    assert service.get(session.id).request == session.request


def test_development_diff_scan_rejects_broker_write_sdk_or_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "AGENTS.md").write_text("# Rules")
    service = DevelopmentService(
        Settings(
            development_repository=repository,
            development_state_directory=Path(".data/development"),
        )
    )
    forbidden_diff = (
        "diff --git a/app/orders.py b/app/orders.py\n"
        "--- /dev/null\n"
        "+++ b/app/orders.py\n"
        "@@ -0,0 +1 @@\n"
        "+client.place_order(symbol, quantity)\n"
    )
    monkeypatch.setattr(service, "_git", lambda *_arguments: forbidden_diff)

    result = service._scan_forbidden_diff(repository)

    assert result["passed"] is False
    assert "place_order" in str(result["output"])


def test_development_staged_scan_rejects_binary_and_symlink_modes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "AGENTS.md").write_text("# Rules")
    service = DevelopmentService(
        Settings(
            development_repository=repository,
            development_state_directory=Path(".data/development"),
        )
    )

    def fake_git(*arguments, **kwargs):
        del kwargs
        if "--name-only" in arguments:
            return "asset.bin\0link.py\0"
        return (
            "diff --git a/asset.bin b/asset.bin\n"
            "GIT binary patch\n"
            "diff --git a/link.py b/link.py\n"
            "new file mode 120000\n"
        )

    monkeypatch.setattr(service, "_git", fake_git)
    result = service._scan_forbidden_diff(repository, cached=True)

    assert result["passed"] is False
    assert "binary change" in str(result["output"])
    assert "changed symlink" in str(result["output"])


def test_development_environment_drops_relative_and_repository_path_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runtime = tmp_path / "runtime"
    for child in ("home", "xdg-config", "xdg-cache", "xdg-data", "codex", "tmp"):
        (runtime / child).mkdir(parents=True, exist_ok=True)
    safe_bin = tmp_path / "safe-bin"
    safe_bin.mkdir()
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((".", str(repository / "bin"), str(safe_bin))),
    )
    service = DevelopmentService(
        Settings(development_repository=repository)
    )

    environment = service._safe_environment(runtime)

    assert environment["PATH"] == str(safe_bin.resolve())


def test_development_rejects_tracked_private_environment_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "AGENTS.md").write_text("# Rules")
    service = DevelopmentService(
        Settings(
            development_repository=repository,
            development_state_directory=Path(".data/development"),
        )
    )
    monkeypatch.setattr(
        service,
        "_git",
        lambda *_arguments: ".env\n",
    )

    with pytest.raises(RuntimeError, match="tracks a private environment"):
        service._verify_repository()


def test_development_rejects_executable_local_git_drivers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "AGENTS.md").write_text("# Rules")
    service = DevelopmentService(
        Settings(development_repository=repository)
    )

    def fake_git(*arguments, **kwargs):
        del kwargs
        if "ls-files" in arguments:
            return ""
        if "--get-regexp" in arguments:
            return "filter.exfil.clean\n"
        return ""

    monkeypatch.setattr(service, "_git", fake_git)

    with pytest.raises(RuntimeError, match="executable filter"):
        service._verify_repository()
