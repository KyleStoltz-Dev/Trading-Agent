import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from app.metatrader_pairing import Pairing, read_pairing
from app.metatrader_refresh import (
    SOURCE,
    RefreshError,
    atomic_private,
    compile_candidate,
    refresh,
)

TOKEN = "synthetic-local-pairing-token-for-tests-only"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="Local updater targets macOS/Wine")
PAIRING = Pairing("123", "Synthetic-Demo", "XAUUSD", 8766, TOKEN)


def preset(path):
    atomic_private(
        path,
        (
            f"ExpectedAccount=123\nExpectedServer=Synthetic-Demo\nQuoteSymbol=XAUUSD\n"
            f"ReceiverPort=8766\nReceiverToken={TOKEN}\n"
        ).encode(),
    )
    return path


def test_private_pairing_roundtrip_and_no_secret_repr(tmp_path):
    assert read_pairing(preset(tmp_path / "test.set")) == PAIRING
    assert TOKEN not in repr(PAIRING)


def test_receiver_reuses_preset_without_prompting_or_echoing_token(tmp_path, monkeypatch, capsys):
    from app.metatrader_companion import run

    path = preset(tmp_path / "test.set")
    monkeypatch.setattr("sys.argv", ["companion", "--preset", str(path)])
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unexpected prompt"))
    called = []
    monkeypatch.setattr("app.metatrader_companion.uvicorn.run", lambda *a, **k: called.append(k))
    run()
    assert called[0]["port"] == 8766
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["permissions", "symlink", "duplicate", "unknown", "large"])
def test_unsafe_pairing_rejected(tmp_path, kind):
    path = preset(tmp_path / "test.set")
    if kind == "permissions":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "link.set"
        link.symlink_to(path)
        path = link
    elif kind in ("duplicate", "unknown"):
        atomic_private(
            path,
            path.read_bytes()
            + (b"ExpectedAccount=321\n" if kind == "duplicate" else b"Password=unused\n"),
        )
    else:
        atomic_private(path, b"x" * 9000)
    with pytest.raises(ValueError):
        read_pairing(path)


@pytest.fixture
def installation(tmp_path):
    experts = tmp_path / "MQL5/Experts"
    experts.mkdir(parents=True)
    atomic_private(experts / "TradingAgentReadOnly.mq5", b"old source")
    atomic_private(experts / "TradingAgentReadOnly.ex5", b"old binary")
    return tmp_path


def update(installation, **kwargs):
    return refresh(
        terminal=installation,
        pairing=PAIRING,
        wine=Path("/fake/wine"),
        prefix=installation,
        compiler=kwargs.pop("compiler", lambda *a: b"new binary"),
        probe=kwargs.pop("probe", lambda p: {"status": "receiving", "companion_build": "legacy"}),
        **kwargs,
    )


def test_legacy_requires_explicit_one_time_bootstrap(installation):
    with pytest.raises(RefreshError, match="bootstrap"):
        update(installation)
    assert (installation / "MQL5/Experts/TradingAgentReadOnly.ex5").read_bytes() == b"old binary"
    assert not (installation / "MQL5/Files/TradingAgentReadOnly.reload").exists()


def test_bootstrap_is_not_reported_as_running(installation):
    result = update(installation, bootstrap=True)
    assert result["status"] == "bootstrap_installed_not_active"
    marker = installation / "MQL5/Files/TradingAgentReadOnly.reload"
    assert marker.read_text() == result["build"] + "\n"
    assert marker.stat().st_mode & 0o077 == 0
    assert (
        installation / "MQL5/Files/TradingAgentUpdate/previous.ex5"
    ).read_bytes() == b"old binary"


def test_live_update_requires_matching_build_ack(installation):
    expected = hashlib.sha256(SOURCE.read_text().encode()).hexdigest()
    replies = iter(
        [
            {"status": "receiving", "companion_build": "a" * 64},
            {"status": "receiving", "companion_build": expected},
        ]
    )
    result = update(installation, probe=lambda _: next(replies))
    assert result == {"status": "updated_and_verified", "build": expected}


def test_already_current_does_not_compile(installation):
    expected = hashlib.sha256(SOURCE.read_text().encode()).hexdigest()

    def compiler(*args):
        pytest.fail("Unexpected compilation")

    result = update(
        installation,
        compiler=compiler,
        probe=lambda _: {
            "status": "receiving",
            "companion_build": expected,
        },
    )
    assert result["status"] == "already_current"


def test_no_ack_restores_files_not_a_false_success(installation):
    with pytest.raises(RefreshError, match="acknowledgement"):
        update(
            installation,
            wait_seconds=0,
            probe=lambda _: {
                "status": "receiving",
                "companion_build": "a" * 64,
            },
        )
    assert (installation / "MQL5/Experts/TradingAgentReadOnly.ex5").read_bytes() == b"old binary"
    assert (installation / "MQL5/Experts/TradingAgentReadOnly.mq5").read_bytes() == b"old source"
    assert not (installation / "MQL5/Files/TradingAgentReadOnly.reload").exists()


def test_compile_failure_leaves_installed_files_unchanged(installation):
    def fail(*args):
        raise RefreshError("Compiler failed")

    with pytest.raises(RefreshError, match="Compiler"):
        update(installation, bootstrap=True, compiler=fail)
    assert (installation / "MQL5/Experts/TradingAgentReadOnly.ex5").read_bytes() == b"old binary"


def test_symlinked_destination_refused(installation, tmp_path):
    original = installation / "MQL5/Experts/TradingAgentReadOnly.ex5"
    original.unlink()
    outside = tmp_path / "unrelated.ex5"
    outside.write_bytes(b"unrelated")
    original.symlink_to(outside)
    with pytest.raises(RefreshError, match="symlink"):
        update(installation, bootstrap=True)
    assert outside.read_bytes() == b"unrelated"


@pytest.mark.parametrize("result", ["success", "errors", "warnings", "timeout", "missing"])
def test_compiler_checks_actual_log_not_exit_code(monkeypatch, result):
    def fake_run(args, **kwargs):
        assert kwargs["timeout"] == 30
        assert "shell" not in kwargs
        if result == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        path = Path(args[2].removeprefix("/compile:Z:").replace("\\", "/"))
        if result != "missing":
            errors = 1 if result == "errors" else 0
            warnings = 1 if result == "warnings" else 0
            path.with_suffix(".log").write_bytes(
                f"Result: {errors} errors, {warnings} warnings".encode("utf-16-le")
            )
            path.with_suffix(".ex5").write_bytes(b"compiled")
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr("app.metatrader_refresh.subprocess.run", fake_run)
    if result == "success":
        assert (
            compile_candidate(Path("/wine"), Path("/editor"), Path("/prefix"), "source")
            == b"compiled"
        )
    else:
        with pytest.raises(RefreshError):
            compile_candidate(Path("/wine"), Path("/editor"), Path("/prefix"), "source")


def test_refresh_marker_cannot_supply_commands_or_expand_permissions():
    source = SOURCE.read_text()
    body = source.split("bool CheckLocalRefresh()", 1)[1].split("string CaptureTime", 1)[0]
    assert "MQL_TRADE_ALLOWED" in body and "MQL_DLLS_ALLOWED" in body
    assert "FileSize(handle) != 65" in body
    assert "PinnedAccount()" in body
    assert "ChartSaveTemplate(0, reload_template)" in body
    assert "ChartApplyTemplate(0, reload_template)" in body
    assert "build == COMPANION_BUILD_ID" in body
    assert "TERMINAL_TRADE_ALLOWED" in body
    assert "TradingAgentReadOnly.reload-attempt" in body
    assert "if(previous == build) return false" in body
    assert "#import" not in source
