import subprocess
from pathlib import Path

import pytest

from app.db import (
    LEGACY_TABLES,
    LEGACY_UNASSIGNED_ACCOUNT_ID,
    LEGACY_WORKSPACE_ID,
    SchemaState,
    _adopt_legacy_database_locked,
    _backup_database,
)


def test_database_backup_uses_exclusive_private_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup = tmp_path / "legacy.dump"
    captured: dict[str, object] = {}
    monkeypatch.setattr("app.db.shutil.which", lambda _name: "/usr/bin/pg_dump")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        kwargs["stdout"].write(b"bounded-test-dump")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("app.db.subprocess.run", fake_run)

    _backup_database(
        backup,
        "postgresql+psycopg://trading:private@localhost:5432/trading_agent",
    )

    assert captured["command"] == ["/usr/bin/pg_dump", "--format=custom"]
    assert backup.read_bytes() == b"bounded-test-dump"
    assert backup.stat().st_mode & 0o777 == 0o600


def test_database_backup_rejects_dangling_symlink(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "legacy.dump"
    try:
        backup.symlink_to(tmp_path / "missing")
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(FileExistsError):
        _backup_database(
            backup,
            "postgresql+psycopg://trading:private@localhost:5432/trading_agent",
        )


def test_legacy_adoption_copies_rows_into_required_workspace_account_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    statements: list[str] = []
    parameters: list[dict | None] = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def begin(self):
            return self

        def execute(self, statement, values=None):
            statements.append(str(statement))
            parameters.append(values)

        def scalar(self, _statement):
            return 0

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(
        "app.db.inspect_schema",
        lambda _engine: SchemaState(
            current_revision=None,
            head_revision="head",
            tables=LEGACY_TABLES,
            legacy_unmanaged=True,
        ),
    )
    monkeypatch.setattr("app.db._backup_database", lambda *_args: None)
    monkeypatch.setattr("app.db.command.upgrade", lambda *_args: None)

    _adopt_legacy_database_locked(
        tmp_path / "legacy.dump",
        "postgresql+psycopg://trading:private@localhost:5432/trading_agent",
        FakeEngine(),
    )

    inserts = "\n".join(item for item in statements if "INSERT INTO public." in item)
    insert_parameters = [
        values
        for statement, values in zip(statements, parameters, strict=True)
        if "INSERT INTO public." in statement
    ]
    assert insert_parameters == [
        {
            "workspace_id": LEGACY_WORKSPACE_ID,
            "account_id": LEGACY_UNASSIGNED_ACCOUNT_ID,
        }
    ] * 4
    assert "(id, workspace_id, account_id" in inserts
    assert "session_id, role, content, status, created_at" in inserts
