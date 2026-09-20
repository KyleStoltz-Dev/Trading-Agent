"""Private settings storage shared by all interfaces.

Hold settings_transaction across a coordinated settings/database change. Regular
writes reuse the same per-path recursive lock, so rollback cannot erase another
thread/process's successful update. This is not a crash-atomic distributed commit.
"""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from filelock import FileLock


@cache
def _file_lock(path: str) -> FileLock:
    return FileLock(path, timeout=10, mode=0o600)


@contextmanager
def settings_lock(path: Path) -> Iterator[None]:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("setup refuses a symlinked environment file")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    if lock_path.is_symlink():
        raise ValueError("setup refuses a symlinked settings lock")
    with _file_lock(str(lock_path)):
        yield


@contextmanager
def settings_transaction(path: Path) -> Iterator[None]:
    """Serialize snapshot, writes, dependent commit and rollback, including cancellation."""
    with settings_lock(path):
        snapshot = snapshot_env_file(path)
        try:
            yield
        except BaseException:
            restore_env_file(path, snapshot)
            raise


SAFE_SETUP_KEYS = frozenset(
    {
        "MODEL_PROVIDER",
        "AGENT_MODEL_PINNED",
        "OPENAI_MODEL",
        "ANTHROPIC_MODEL",
        "OPENAI_AUTH_MODE",
        "ANTHROPIC_AUTH_MODE",
        "OLLAMA_BASE_URL",
        "OLLAMA_MODEL",
        "OLLAMA_ECONOMY_MODEL",
        "OLLAMA_BALANCED_MODEL",
        "OLLAMA_DEEP_MODEL",
        "OLLAMA_CONTEXT_LENGTH",
        "STARTUP_MODEL_SMOKE_TEST",
        "LOCAL_SERVICE_AUTOSTART",
        "POSTGRES_SERVICE_NAME",
        "DATABASE_MODE",
        "TRADING_WORKSPACE",
        "TRADING_ACCOUNT",
        "MAXIMUM_TRADE_RISK_PERCENT",
        "BROKER_PROVIDER",
        "METATRADER_PLATFORM",
        "NEWS_PROVIDER",
        "TRADINGVIEW_WEBHOOK_ENABLED",
    }
)


def update_env_file(path: Path, values: dict[str, str]) -> None:
    unknown = set(values) - SAFE_SETUP_KEYS
    if unknown:
        raise ValueError(f"setup cannot write unsupported settings: {sorted(unknown)}")
    if any(any(character in value for character in "\r\n\0") for value in values.values()):
        raise ValueError("setup values cannot contain control characters")
    if any("${" in value for value in values.values()):
        raise ValueError("setup values cannot interpolate environment variables")

    with settings_lock(path):
        _update_env_file_locked(path, values)


def _update_env_file_locked(path: Path, values: dict[str, str]) -> None:
    """Serialize the entire read/merge/replace operation across running interfaces."""

    if path.is_symlink():
        raise ValueError("setup refuses to read or replace a symlinked environment file")
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    output: list[str] = []
    written: set[str] = set()
    for line in existing:
        key, separator, _ = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
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
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(0o600)


def snapshot_env_file(path: Path) -> bytes | None:
    """Capture a private env file so a coordinated database write can be undone."""
    if path.is_symlink():
        raise ValueError("setup refuses to read or replace a symlinked environment file")
    return path.read_bytes() if path.exists() else None


def restore_env_file(path: Path, snapshot: bytes | None) -> None:
    """Restore an env snapshot atomically after a related database failure."""
    lock_path = path.expanduser().absolute().with_name(f".{path.name}.lock")
    if not _file_lock(str(lock_path)).is_locked:
        raise RuntimeError("settings rollback requires an active settings transaction")
    if path.is_symlink():
        raise ValueError("setup refuses to read or replace a symlinked environment file")
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".restore",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(snapshot)
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(0o600)
