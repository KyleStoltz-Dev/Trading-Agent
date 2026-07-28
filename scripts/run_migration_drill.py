#!/usr/bin/env python3
"""Exercise clean upgrade, reversible-tail downgrade, re-upgrade, and restore.

This script is destructive to the named drill database. It refuses to start unless the exact
database name is repeated through --confirm-disposable-database.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from scripts.verify_postgres_backup_restore import (
    BackupRestoreError,
    verify_backup_restore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVERSIBLE_FLOOR = "a73f1c9d4e20"


class MigrationDrillError(RuntimeError):
    pass


def _run_alembic(*arguments: str, environment: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603 - sys.executable and Alembic args are fixed.
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    )
    if result.returncode:
        raise MigrationDrillError(f"alembic {' '.join(arguments)} failed")


def _head_and_floor() -> tuple[str, str]:
    config = Config(PROJECT_ROOT / "alembic.ini")
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if len(heads) != 1:
        raise MigrationDrillError(f"expected one migration head, found {heads}")
    revisions = {
        revision.revision
        for revision in script.walk_revisions("base", heads[0])
    }
    if REVERSIBLE_FLOOR not in revisions:
        raise MigrationDrillError(
            f"reviewed reversible floor {REVERSIBLE_FLOOR} is not an ancestor of head"
        )
    return heads[0], REVERSIBLE_FLOOR


def run_drill(database_url: str, *, confirmed_database: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql") or not url.database:
        raise MigrationDrillError("migration drill requires a PostgreSQL DATABASE_URL")
    if url.database != confirmed_database:
        raise MigrationDrillError(
            "confirmation must exactly match the disposable DATABASE_URL database name"
        )
    if not confirmed_database.startswith("trading_agent_release_"):
        raise MigrationDrillError(
            "disposable drill database name must start with 'trading_agent_release_'"
        )
    head, floor = _head_and_floor()
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment["DATABASE_AUTO_MIGRATE"] = "false"

    _run_alembic("upgrade", "head", environment=environment)
    try:
        verify_backup_restore(database_url)
    except BackupRestoreError as exc:
        raise MigrationDrillError(str(exc)) from exc
    _run_alembic("downgrade", floor, environment=environment)
    _run_alembic("upgrade", head, environment=environment)
    _run_alembic("check", environment=environment)
    print(f"Migration drill passed: {floor} -> {head}, restore verified")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-disposable-database",
        required=True,
        help="repeat the exact database name selected by DATABASE_URL",
    )
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required; do not place credentials in arguments")
    try:
        run_drill(
            database_url,
            confirmed_database=args.confirm_disposable_database,
        )
    except MigrationDrillError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
