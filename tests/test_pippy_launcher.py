from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services import pippy_launcher


def _pippy_project(root: Path) -> Path:
    project = root / "pippy"
    for python in (
        project / ".venv" / "bin" / "python",
        project / ".venv" / "Scripts" / "python.exe",
    ):
        python.parent.mkdir(parents=True)
        python.touch()
    (project / "pyproject.toml").write_text("[project]\nname='pippy'\n")
    return project


def test_launch_plan_shares_one_ephemeral_key_without_writing_it(tmp_path, monkeypatch) -> None:
    project = _pippy_project(tmp_path)
    monkeypatch.setenv("PATH", "/safe/runtime/path")
    monkeypatch.setenv("DATABASE_URL", "postgresql://private-database")
    monkeypatch.setenv("OANDA_API_TOKEN", "private-broker-token")
    monkeypatch.setenv("OPENAI_API_KEY", "private-model-token")
    monkeypatch.setattr(
        pippy_launcher.secrets,
        "token_urlsafe",
        lambda _length: "temporary-private-key-value-1234567890",
    )

    plan = pippy_launcher.build_pippy_launch_plan(
        trading_directory=tmp_path,
        pippy_directory=project,
    )

    assert plan.trading_environment["TRADING_AGENT_API_KEY"] == (
        "temporary-private-key-value-1234567890"
    )
    assert plan.trading_environment["TRADING_DASHBOARD_AUTOCONNECT"] == "true"
    assert plan.trading_environment["DATABASE_URL"] == "postgresql://private-database"
    assert plan.pippy_environment["PATH"] == "/safe/runtime/path"
    assert plan.pippy_environment["PYTHONUNBUFFERED"] == "1"
    assert plan.pippy_environment["PIPPY_TRADING_AGENT_URL"] == (
        "http://127.0.0.1:8000"
    )
    assert plan.pippy_environment["TRADING_AGENT_API_KEY"] == (
        plan.trading_environment["TRADING_AGENT_API_KEY"]
    )
    assert set(plan.pippy_environment) <= {
        *pippy_launcher._SAFE_PIPPY_RUNTIME_VARIABLES,
        "PYTHONUNBUFFERED",
        "TRADING_AGENT_API_KEY",
        "PIPPY_TRADING_AGENT_URL",
    }
    assert "DATABASE_URL" not in plan.pippy_environment
    assert "OANDA_API_TOKEN" not in plan.pippy_environment
    assert "OPENAI_API_KEY" not in plan.pippy_environment
    assert plan.trading_command[-1] == "8000"
    assert plan.pippy_command[-1] == "8001"
    assert not (project / ".env").exists()


def test_resolve_pippy_python_uses_platform_virtual_environment_layout(
    tmp_path: Path,
) -> None:
    project = _pippy_project(tmp_path)

    assert pippy_launcher._resolve_pippy_python(project, platform_name="posix") == (
        project / ".venv" / "bin" / "python"
    )
    assert pippy_launcher._resolve_pippy_python(project, platform_name="nt") == (
        project / ".venv" / "Scripts" / "python.exe"
    )


def test_resolve_pippy_python_reports_platform_specific_missing_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        pippy_launcher.PippyLaunchError,
        match=r"Scripts[/\\\\]python\.exe",
    ):
        pippy_launcher._resolve_pippy_python(tmp_path, platform_name="nt")


def test_launch_plan_rejects_shared_port(tmp_path) -> None:
    project = _pippy_project(tmp_path)

    with pytest.raises(pippy_launcher.PippyLaunchError, match="different ports"):
        pippy_launcher.build_pippy_launch_plan(
            trading_directory=tmp_path,
            pippy_directory=project,
            trading_port=8001,
            pippy_port=8001,
        )


def test_run_stack_passes_each_process_only_its_own_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _pippy_project(tmp_path)
    plan = pippy_launcher.build_pippy_launch_plan(
        trading_directory=tmp_path,
        pippy_directory=project,
    )
    trading_process = Mock()
    trading_process.poll.return_value = None
    pippy_process = Mock()
    pippy_process.poll.return_value = 7
    popen = Mock(side_effect=(trading_process, pippy_process))
    monkeypatch.setattr(pippy_launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(pippy_launcher, "_wait_until_ready", lambda *_args: None)
    monkeypatch.setattr(pippy_launcher, "_stop_process", lambda _process: None)

    with pytest.raises(pippy_launcher.PippyLaunchError, match="Pippy stopped"):
        pippy_launcher.run_pippy_stack(plan, open_browser=False)

    assert popen.call_args_list[0].kwargs["env"] is plan.trading_environment
    assert popen.call_args_list[1].kwargs["env"] is plan.pippy_environment
