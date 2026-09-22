"""Explicit local updates of our own EA only; not an agent tool or remote executor."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from filelock import FileLock, Timeout

from app.metatrader_pairing import Pairing, read_pairing

SOURCE = Path(__file__).with_name("companions") / "TradingAgentReadOnly.mq5"
EA_NAME = "TradingAgentReadOnly"
BUILD_LINE = '#define COMPANION_BUILD_ID "development"'


class RefreshError(RuntimeError):
    pass


def regular(path: Path, *, limit: int = 8_388_608) -> bytes:
    if path.resolve() != path.absolute() or path.is_symlink():
        raise RefreshError("Refusing a symlinked update path")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise RefreshError("Invalid update file")
    if os.name != "nt" and info.st_uid != os.getuid():
        raise RefreshError("Update files must belong to the current user")
    return path.read_bytes()


def atomic_private(path: Path, data: bytes) -> None:
    if path.parent.resolve() != path.parent.absolute() or path.is_symlink():
        raise RefreshError("Refusing a symlinked update destination")
    if path.exists():
        regular(path)
    fd, temporary = tempfile.mkstemp(prefix=".trading-agent-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def health(pairing: Pairing) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{pairing.port}/v1/companion/health",
        headers={"Authorization": f"Bearer {pairing.token}"},
    )

    # Never forward the private token through redirects or environment proxies.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=3) as response:
            raw = response.read(8193)
        if len(raw) > 8192:
            raise RefreshError("Receiver response is too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise RefreshError("Invalid receiver response")
        return result
    except (OSError, ValueError, urllib.error.URLError):
        raise RefreshError("Local receiver unavailable; start it with the saved --preset") from None


def compile_candidate(wine: Path, editor: Path, prefix: Path, source: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="trading-agent-mt5-build-") as temporary:
        directory = Path(temporary).resolve()
        candidate = directory / f"{EA_NAME}.mq5"
        atomic_private(candidate, source.encode())
        windows_path = "Z:" + str(candidate).replace("/", "\\")
        env = {
            **os.environ,
            "WINEPREFIX": str(prefix),
            "WINEDEBUG": "-all",
            "MVK_CONFIG_LOG_LEVEL": "0",
        }
        try:
            subprocess.run(  # noqa: S603 -- fixed vendor compiler, argv list, no shell
                [str(wine), str(editor), f"/compile:{windows_path}", "/log"],
                env=env,
                timeout=30,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise RefreshError(
                "MetaEditor compilation failed or timed out; installed EA unchanged"
            ) from None
        log = candidate.with_suffix(".log")
        binary = candidate.with_suffix(".ex5")
        if not log.exists() or not binary.exists():
            raise RefreshError("MetaEditor did not produce a compilation log and binary")
        report = regular(log, limit=4_194_304).decode("utf-16-le", errors="replace")
        # MetaEditor under Wine can return 1 on success. Require the actual final
        # compiler result and a fresh nonempty binary in this unique directory.
        results = re.findall(r"Result:\s*(\d+) errors?,\s*(\d+) warnings?", report)
        if not results or results[-1] != ("0", "0"):
            raise RefreshError("Compilation has errors or warnings; installed EA unchanged")
        artifact = regular(binary)
        if not artifact:
            raise RefreshError("Compiler output is empty")
        return artifact


def refresh(
    *,
    terminal: Path,
    pairing: Pairing,
    wine: Path,
    prefix: Path,
    bootstrap: bool = False,
    compiler=compile_candidate,
    probe=health,
    wait_seconds: float = 40,
) -> dict:
    if terminal.resolve() != terminal.absolute() or not terminal.is_dir():
        raise RefreshError("Use the real MT5 data/installation directory, without symlinks")
    experts = terminal / "MQL5/Experts"
    files = terminal / "MQL5/Files"
    # Current terminals use MQL5/Profiles; some documented/older layouts use
    # Profiles directly. Reserve the fixed private filename in both locations.
    templates = (terminal / "MQL5/Profiles/Templates", terminal / "Profiles/Templates")
    for directory in (experts, files, *templates):
        if directory.resolve() != directory.absolute():
            raise RefreshError("Refusing a symlinked MT5 directory")
        directory.mkdir(parents=True, exist_ok=True)
    state = files / "TradingAgentUpdate"
    if state.is_symlink():
        raise RefreshError("Refusing a symlinked update state")
    state.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt" and (state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077):
        raise RefreshError("Update state must be a private current-user directory")
    with FileLock(state / "refresh.lock", timeout=0):
        installed_source = experts / f"{EA_NAME}.mq5"
        installed_binary = experts / f"{EA_NAME}.ex5"
        old_source, old_binary = regular(installed_source), regular(installed_binary)
        canonical = SOURCE.read_text()
        if canonical.count(BUILD_LINE) != 1:
            raise RefreshError("Bundled companion build marker is missing or ambiguous")
        build = hashlib.sha256(canonical.encode()).hexdigest()
        before = probe(pairing)
        if before.get("status") == "receiving" and before.get("companion_build") == build:
            return {"status": "already_current", "build": build}
        ready = before.get("status") == "receiving" and bool(
            re.fullmatch(
                r"development|[a-f0-9]{64}",
                str(before.get("companion_build", "")),
            )
        )
        if not ready and not bootstrap:
            raise RefreshError(
                "Current EA lacks refresh support or is not connected; one-time bootstrap required"
            )
        candidate = canonical.replace(BUILD_LINE, f'#define COMPANION_BUILD_ID "{build}"')
        binary = compiler(wine, terminal / "MetaEditor64.exe", prefix, candidate)
        atomic_private(state / "previous.mq5", old_source)
        atomic_private(state / "previous.ex5", old_binary)
        marker = files / f"{EA_NAME}.reload"
        old_marker = regular(marker, limit=1024) if marker.exists() else None
        attempted = files / f"{EA_NAME}.reload-attempt"
        old_attempt = regular(attempted, limit=1024) if attempted.exists() else b""
        # ChartSaveTemplate uses Profiles/Templates. Pre-create a mode-0600 file
        # because the template will contain this chart's already-paired token.
        for directory in templates:
            template = directory / f"{EA_NAME}-reload.tpl"
            atomic_private(template, regular(template) if template.exists() else b"")
        try:
            atomic_private(installed_source, candidate.encode())
            atomic_private(installed_binary, binary)
            atomic_private(attempted, b"")
            atomic_private(marker, (build + "\n").encode())
            if not ready:
                return {"status": "bootstrap_installed_not_active", "build": build}
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                try:
                    current = probe(pairing)
                except RefreshError:
                    current = {}
                if current.get("status") == "receiving" and current.get("companion_build") == build:
                    return {"status": "updated_and_verified", "build": build}
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            raise RefreshError(
                "No new-build acknowledgement; previous files restored. Check MT5 before retrying"
            )
        except BaseException:
            atomic_private(installed_source, old_source)
            atomic_private(installed_binary, old_binary)
            atomic_private(attempted, old_attempt)
            if old_marker is not None:
                atomic_private(marker, old_marker)
            elif marker.exists():
                marker.unlink()
            # Restoring files is not proof that MT5 reloaded the prior instance.
            raise


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh only Trading Agent's read-only MT5 companion"
    )
    parser.add_argument("--preset", required=True, type=Path)
    parser.add_argument("--terminal", required=True, type=Path)
    parser.add_argument("--wine-prefix", required=True, type=Path)
    parser.add_argument(
        "--wine",
        type=Path,
        default=Path(
            "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine",
        ),
    )
    parser.add_argument(
        "--bootstrap", action="store_true", help="Install first refresh-capable build"
    )
    args = parser.parse_args()
    try:
        result = refresh(
            terminal=args.terminal,
            pairing=read_pairing(args.preset),
            wine=args.wine,
            prefix=args.wine_prefix,
            bootstrap=args.bootstrap,
        )
    except Timeout:
        parser.exit(1, "Another companion update is already running.\n")
    except (RefreshError, OSError, ValueError) as exc:
        parser.exit(1, f"Companion update stopped: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    run()
