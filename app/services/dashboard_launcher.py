"""Launch the local Trading-Agent dashboard with one ephemeral browser session."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DashboardLaunchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DashboardLaunchPlan:
    command: tuple[str, ...]
    directory: Path
    environment: dict[str, str]
    service_url: str
    browser_url: str


def _service_is_reachable(url: str, *, timeout_seconds: float = 0.25) -> bool:
    try:
        # Launch plans construct this URL from a validated port and loopback host.
        with urllib.request.urlopen(  # noqa: S310
            f"{url}/health", timeout=timeout_seconds
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def build_dashboard_launch_plan(
    *,
    trading_directory: Path,
    port: int = 8000,
) -> DashboardLaunchPlan:
    """Create a local launch plan without persisting or logging its session key."""

    if not 1 <= port <= 65535:
        raise DashboardLaunchError("port must be between 1 and 65535")
    trading_root = trading_directory.expanduser().resolve()
    if not (trading_root / "pyproject.toml").is_file():
        raise DashboardLaunchError(
            f"Trading-Agent was not found at {trading_root}."
        )
    api_key = secrets.token_urlsafe(32)
    bootstrap_token = secrets.token_urlsafe(32)
    service_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "TRADING_AGENT_API_KEY": api_key,
            "TRADING_DASHBOARD_AUTOCONNECT": "true",
            "TRADING_DASHBOARD_BOOTSTRAP_TOKEN": bootstrap_token,
        }
    )
    return DashboardLaunchPlan(
        command=(
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ),
        directory=trading_root,
        environment=environment,
        service_url=service_url,
        browser_url=f"{service_url}/#session={bootstrap_token}",
    )


def _wait_until_ready(
    process: subprocess.Popen[Any],
    url: str,
    *,
    timeout_seconds: float = 20,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise DashboardLaunchError(
                f"Trading-Agent stopped during startup with exit code {return_code}"
            )
        try:
            # Launch plans construct this URL from a validated port and loopback host.
            with urllib.request.urlopen(  # noqa: S310
                f"{url}/health", timeout=0.5
            ) as response:
                if response.status == 200:
                    time.sleep(0.2)
                    return_code = process.poll()
                    if return_code is None:
                        return
                    raise DashboardLaunchError(
                        "Trading-Agent could not claim the dashboard port; "
                        f"its server stopped with exit code {return_code}"
                    )
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    raise DashboardLaunchError(
        f"Trading-Agent did not become ready within {timeout_seconds:g} seconds"
    )


def run_dashboard(plan: DashboardLaunchPlan, *, open_browser: bool = True) -> None:
    """Run the local dashboard until interrupted, then stop its child process."""

    if _service_is_reachable(plan.service_url):
        raise DashboardLaunchError(
            f"Dashboard port is already in use: {plan.service_url}"
        )
    # The command is assembled internally from sys.executable and fixed arguments.
    process = subprocess.Popen(  # noqa: S603
        plan.command,
        cwd=plan.directory,
        env=plan.environment,
    )
    try:
        _wait_until_ready(process, plan.service_url)
        if open_browser:
            webbrowser.open(plan.browser_url)
        while True:
            return_code = process.poll()
            if return_code is not None:
                raise DashboardLaunchError(
                    f"Trading-Agent stopped with exit code {return_code}"
                )
            time.sleep(0.25)
    except KeyboardInterrupt:
        return
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
