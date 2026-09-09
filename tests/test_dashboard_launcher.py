from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services import dashboard_launcher


def test_dashboard_plan_uses_one_ephemeral_fragment_key_without_writing_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='trading-agent'\n")
    api_key = "a" * 43
    bootstrap_token = "b" * 43
    monkeypatch.setattr(
        dashboard_launcher.secrets,
        "token_urlsafe",
        Mock(side_effect=(api_key, bootstrap_token)),
    )

    plan = dashboard_launcher.build_dashboard_launch_plan(
        trading_directory=tmp_path,
        port=8765,
    )

    assert plan.environment["TRADING_AGENT_API_KEY"] == api_key
    assert plan.environment["TRADING_DASHBOARD_AUTOCONNECT"] == "true"
    assert (
        plan.environment["TRADING_DASHBOARD_BOOTSTRAP_TOKEN"]
        == bootstrap_token
    )
    assert plan.service_url == "http://127.0.0.1:8765"
    assert plan.browser_url == (
        f"http://127.0.0.1:8765/#session={bootstrap_token}"
    )
    assert plan.command[-1] == "8765"
    assert not (tmp_path / ".env").exists()


def test_dashboard_plan_rejects_invalid_project_or_port(tmp_path: Path) -> None:
    with pytest.raises(dashboard_launcher.DashboardLaunchError, match="between 1 and"):
        dashboard_launcher.build_dashboard_launch_plan(
            trading_directory=tmp_path,
            port=0,
        )

    with pytest.raises(dashboard_launcher.DashboardLaunchError, match="was not found"):
        dashboard_launcher.build_dashboard_launch_plan(
            trading_directory=tmp_path,
            port=8000,
        )


def test_dashboard_launcher_rejects_an_existing_service_before_starting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='trading-agent'\n")
    plan = dashboard_launcher.build_dashboard_launch_plan(
        trading_directory=tmp_path,
        port=8765,
    )
    popen = Mock(side_effect=AssertionError("must not start a competing server"))
    monkeypatch.setattr(dashboard_launcher, "_service_is_reachable", lambda _url: True)
    monkeypatch.setattr(dashboard_launcher.subprocess, "Popen", popen)

    with pytest.raises(dashboard_launcher.DashboardLaunchError, match="already in use"):
        dashboard_launcher.run_dashboard(plan, open_browser=False)

    popen.assert_not_called()
