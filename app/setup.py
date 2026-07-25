import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from sqlalchemy import Engine

from app.config import Settings

ProviderName = Literal["openai", "anthropic", "ollama"]
OLLAMA_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]{0,199}")

SAFE_SETUP_KEYS = frozenset(
    {
        "MODEL_PROVIDER",
        "OLLAMA_BASE_URL",
        "OLLAMA_MODEL",
        "OLLAMA_ECONOMY_MODEL",
        "OLLAMA_BALANCED_MODEL",
        "OLLAMA_DEEP_MODEL",
        "OLLAMA_CONTEXT_LENGTH",
        "STARTUP_MODEL_SMOKE_TEST",
        "LOCAL_SERVICE_AUTOSTART",
        "POSTGRES_SERVICE_NAME",
    }
)


def update_env_file(path: Path, values: dict[str, str]) -> None:
    unknown = set(values) - SAFE_SETUP_KEYS
    if unknown:
        raise ValueError(f"setup cannot write unsupported settings: {sorted(unknown)}")
    if any(any(character in value for character in "\r\n\0") for value in values.values()):
        raise ValueError("setup values cannot contain control characters")

    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    output: list[str] = []
    written: set[str] = set()
    for line in existing:
        key, separator, _ = line.partition("=")
        if separator and key in pending:
            if key not in written:
                output.append(f"{key}={pending[key]}")
                written.add(key)
            continue
        output.append(line)
    for key, value in pending.items():
        if key not in written:
            output.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def provider_settings(provider: ProviderName, model: str = "qwen3.5:9b") -> dict[str, str]:
    if not OLLAMA_MODEL_PATTERN.fullmatch(model):
        raise ValueError("Ollama model name contains unsupported characters")
    values = {"MODEL_PROVIDER": provider}
    if provider == "ollama":
        values.update(
            {
                "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
                "OLLAMA_MODEL": model,
                "OLLAMA_ECONOMY_MODEL": model,
                "OLLAMA_BALANCED_MODEL": model,
                "OLLAMA_DEEP_MODEL": model,
                "OLLAMA_CONTEXT_LENGTH": "16384",
                "STARTUP_MODEL_SMOKE_TEST": "true",
                "LOCAL_SERVICE_AUTOSTART": "true",
            }
        )
    return values


def install_user_launcher(target: Path, launcher_name: str = "trade") -> Path:
    if not target.exists():
        raise FileNotFoundError(f"launcher target does not exist: {target}")
    bin_directory = Path.home() / ".local" / "bin"
    bin_directory.mkdir(parents=True, exist_ok=True)
    launcher = bin_directory / launcher_name
    if launcher.exists() and not launcher.is_symlink():
        raise FileExistsError(f"refusing to replace existing file: {launcher}")
    if launcher.is_symlink():
        launcher.unlink()
    launcher.symlink_to(target.resolve())
    return launcher


def _homebrew() -> str | None:
    return shutil.which("brew")


def start_homebrew_service(service_name: str) -> tuple[bool, str]:
    brew = _homebrew()
    if brew is None:
        return False, "Homebrew is not installed"
    result = subprocess.run(  # noqa: S603
        [brew, "services", "start", service_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    detail = (result.stdout or result.stderr).strip()
    return result.returncode == 0, detail[:500]


def pull_ollama_model(model: str) -> tuple[bool, str]:
    ollama = shutil.which("ollama")
    if ollama is None:
        return False, "Ollama is not installed"
    result = subprocess.run(  # noqa: S603
        [ollama, "pull", model],
        check=False,
        text=True,
        timeout=1800,
    )
    return result.returncode == 0, f"ollama pull exited with status {result.returncode}"


def _ollama_ready(base_url: str) -> bool:
    try:
        with httpx.Client(timeout=1.0, trust_env=False) as client:
            response = client.get(f"{base_url.rstrip('/')}/api/tags")
            return response.is_success
    except httpx.HTTPError:
        return False


def _database_ready(engine: Engine) -> bool:
    try:
        with engine.connect():
            return True
    except Exception:
        return False


def ensure_local_services(settings: Settings, engine: Engine) -> tuple[str, ...]:
    if not settings.local_service_autostart:
        return ()

    messages: list[str] = []
    database_host = urlparse(settings.database_url.replace("+psycopg", "")).hostname
    if database_host in {"localhost", "127.0.0.1", "::1"} and not _database_ready(engine):
        ok, detail = start_homebrew_service(settings.postgres_service_name)
        messages.append(
            f"PostgreSQL {'start requested' if ok else 'could not start'}: {detail}"
        )

    if settings.model_provider == "ollama" and not _ollama_ready(settings.ollama_base_url):
        ok, detail = start_homebrew_service("ollama")
        messages.append(f"Ollama {'start requested' if ok else 'could not start'}: {detail}")

    if messages:
        for _ in range(20):
            database_ok = database_host not in {"localhost", "127.0.0.1", "::1"} or _database_ready(
                engine
            )
            ollama_ok = settings.model_provider != "ollama" or _ollama_ready(
                settings.ollama_base_url
            )
            if database_ok and ollama_ok:
                break
            time.sleep(0.25)
    return tuple(messages)


def shell_path_hint(launcher: Path) -> str | None:
    directory = str(launcher.parent)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if directory in path_entries:
        return None
    shell_file = "~/.zprofile" if sys.platform == "darwin" else "~/.profile"
    return f'Add `export PATH="$HOME/.local/bin:$PATH"` to {shell_file}, then reopen Terminal.'
