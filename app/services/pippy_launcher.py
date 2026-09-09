"""Launch Trading Agent and Pippy with one ephemeral local credential."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PippyLaunchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PippyLaunchPlan:
    trading_command: tuple[str, ...]
    trading_directory: Path
    trading_environment: dict[str, str]
    pippy_command: tuple[str, ...]
    pippy_directory: Path
    pippy_environment: dict[str, str]
    trading_url: str
    pippy_url: str


_SAFE_PIPPY_RUNTIME_VARIABLES = (
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SystemRoot",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "WINDIR",
)


def _resolve_pippy_python(
    pippy_root: Path,
    *,
    platform_name: str | None = None,
) -> Path:
    """Return the Pippy virtual-environment interpreter for this platform."""

    platform_name = platform_name or os.name
    windows_python = pippy_root / ".venv" / "Scripts" / "python.exe"
    posix_python = pippy_root / ".venv" / "bin" / "python"
    expected = windows_python if platform_name == "nt" else posix_python
    if expected.is_file():
        return expected
    raise PippyLaunchError(
        f"Pippy's virtual environment is missing at {expected}."
    )


def _safe_pippy_environment(
    parent_environment: Mapping[str, str],
    *,
    api_key: str,
    trading_url: str,
) -> dict[str, str]:
    """Build the presentation process environment without application secrets."""

    environment = {
        name: parent_environment[name]
        for name in _SAFE_PIPPY_RUNTIME_VARIABLES
        if name in parent_environment
    }
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "TRADING_AGENT_API_KEY": api_key,
            "PIPPY_TRADING_AGENT_URL": trading_url,
        }
    )
    return environment


def build_pippy_launch_plan(
    *,
    trading_directory: Path,
    pippy_directory: Path | None = None,
    trading_port: int = 8000,
    pippy_port: int = 8001,
) -> PippyLaunchPlan:
    """Build a two-process plan without writing its generated credential to disk."""

    trading_root = trading_directory.expanduser().resolve()
    pippy_root = (pippy_directory or Path.home() / "Projects" / "pippy").expanduser().resolve()
    if not (pippy_root / "pyproject.toml").is_file():
        raise PippyLaunchError(
            f"Pippy was not found at {pippy_root}. Use --pippy-directory to select it."
        )
    pippy_python = _resolve_pippy_python(pippy_root)
    if not 1 <= trading_port <= 65535 or not 1 <= pippy_port <= 65535:
        raise PippyLaunchError("ports must be between 1 and 65535")
    if trading_port == pippy_port:
        raise PippyLaunchError("Trading Agent and Pippy must use different ports")

    trading_url = f"http://127.0.0.1:{trading_port}"
    pippy_url = f"http://127.0.0.1:{pippy_port}"
    api_key = secrets.token_urlsafe(32)
    trading_environment = os.environ.copy()
    trading_environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "TRADING_AGENT_API_KEY": api_key,
            "TRADING_DASHBOARD_AUTOCONNECT": "true",
        }
    )
    pippy_environment = _safe_pippy_environment(
        os.environ,
        api_key=api_key,
        trading_url=trading_url,
    )
    return PippyLaunchPlan(
        trading_command=(
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(trading_port),
        ),
        trading_directory=trading_root,
        trading_environment=trading_environment,
        pippy_command=(
            str(pippy_python),
            "-m",
            "uvicorn",
            "pippy.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(pippy_port),
        ),
        pippy_directory=pippy_root,
        pippy_environment=pippy_environment,
        trading_url=trading_url,
        pippy_url=pippy_url,
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
            raise PippyLaunchError(
                f"{url} stopped during startup with exit code {return_code}"
            )
        try:
            # Launch plans construct this URL from validated ports and loopback hosts.
            with urllib.request.urlopen(  # noqa: S310
                f"{url}/health", timeout=0.5
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    raise PippyLaunchError(f"{url} did not become ready within {timeout_seconds:g} seconds")


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_pippy_stack(plan: PippyLaunchPlan, *, open_browser: bool = True) -> None:
    """Run both services until interrupted, then stop both child processes."""

    # Both commands are assembled internally from validated executable paths.
    trading_process = subprocess.Popen(  # noqa: S603
        plan.trading_command,
        cwd=plan.trading_directory,
        env=plan.trading_environment,
    )
    pippy_process: subprocess.Popen[Any] | None = None
    try:
        _wait_until_ready(trading_process, plan.trading_url)
        pippy_process = subprocess.Popen(  # noqa: S603
            plan.pippy_command,
            cwd=plan.pippy_directory,
            env=plan.pippy_environment,
        )
        _wait_until_ready(pippy_process, plan.pippy_url)
        if open_browser:
            webbrowser.open(plan.pippy_url)
        while True:
            trading_code = trading_process.poll()
            pippy_code = pippy_process.poll()
            if trading_code is not None:
                raise PippyLaunchError(
                    f"Trading Agent stopped with exit code {trading_code}"
                )
            if pippy_code is not None:
                raise PippyLaunchError(f"Pippy stopped with exit code {pippy_code}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        return
    finally:
        if pippy_process is not None:
            _stop_process(pippy_process)
        _stop_process(trading_process)
