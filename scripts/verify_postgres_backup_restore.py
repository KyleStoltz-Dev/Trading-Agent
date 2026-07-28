#!/usr/bin/env python3
"""Verify a PostgreSQL custom backup by restoring it into a temporary database.

The source database is read-only throughout the drill. A uniquely named verification
database is created on the same server, compared, and dropped in a finally block.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

PG_ENVIRONMENT_KEYS = {
    "PGCHANNELBINDING": "channel_binding",
    "PGCONNECT_TIMEOUT": "connect_timeout",
    "PGGSSENCMODE": "gssencmode",
    "PGSSLCERT": "sslcert",
    "PGSSLCRL": "sslcrl",
    "PGSSLKEY": "sslkey",
    "PGSSLMODE": "sslmode",
    "PGSSLROOTCERT": "sslrootcert",
}
TEMP_DATABASE_PREFIX = "trading_agent_restore_"


class BackupRestoreError(RuntimeError):
    """The backup could not be proven restorable without touching the source."""


def _validated_url(value: str) -> URL:
    url = make_url(value)
    if not url.drivername.startswith("postgresql"):
        raise BackupRestoreError("backup verification requires a PostgreSQL DATABASE_URL")
    if not url.database:
        raise BackupRestoreError("DATABASE_URL must select a source database")
    return url


def _connection_kwargs(url: URL, *, database: str | None = None) -> dict[str, str | int]:
    values: dict[str, str | int] = {"dbname": database or str(url.database)}
    if url.host:
        values["host"] = url.host
    if url.port:
        values["port"] = url.port
    if url.username:
        values["user"] = url.username
    if url.password:
        values["password"] = url.password
    for option in PG_ENVIRONMENT_KEYS.values():
        raw = url.query.get(option)
        if raw is not None:
            values[option] = str(raw)
    return values


def _postgres_environment(url: URL, *, database: str | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PG") and key != "DATABASE_URL"
    }
    values = _connection_kwargs(url, database=database)
    mapping = {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "dbname": "PGDATABASE",
    }
    for source, target in mapping.items():
        if source in values:
            environment[target] = str(values[source])
    for variable, option in PG_ENVIRONMENT_KEYS.items():
        if option in values:
            environment[variable] = str(values[option])
    return environment


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise BackupRestoreError(f"{name} is required for backup verification")
    return executable


def _run(command: list[str], *, environment: dict[str, str], stdin=None, stdout=None) -> None:
    result = subprocess.run(  # noqa: S603 - executables are resolved with shutil.which.
        command,
        env=environment,
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        check=False,
        text=stdout is None,
    )
    if result.returncode:
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no diagnostic"
        raise BackupRestoreError(f"{Path(command[0]).name} failed: {detail[:500]}")


def _public_table_counts(connection: psycopg.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
        """
    ).fetchall()
    counts: dict[str, int] = {}
    for (table,) in rows:
        query = sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier("public"),
            sql.Identifier(str(table)),
        )
        counts[str(table)] = int(connection.execute(query).fetchone()[0])
    return counts


def _alembic_revisions(connection: psycopg.Connection) -> tuple[str, ...]:
    exists = connection.execute(
        "SELECT to_regclass('public.alembic_version')"
    ).fetchone()[0]
    if exists is None:
        return ()
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).fetchall()
    )


def _create_database(admin: psycopg.Connection, name: str) -> None:
    admin.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
    )


def _drop_database(admin: psycopg.Connection, name: str) -> None:
    admin.execute(
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    )


@contextmanager
def _backup_destination(path: Path | None) -> Iterator[tuple[Path, bool]]:
    if path is None:
        with tempfile.TemporaryDirectory(prefix="trading-agent-backup-drill-") as directory:
            yield Path(directory) / "verification.dump", False
        return
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise BackupRestoreError("backup path must be absolute")
    if expanded.exists() or expanded.is_symlink():
        raise BackupRestoreError("backup path must not already exist")
    if (
        not expanded.parent.is_dir()
        or expanded.parent.is_symlink()
        or expanded.parent.resolve() != expanded.parent.absolute()
    ):
        raise BackupRestoreError("backup parent must be a real directory")
    yield expanded, True


def _open_backup(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


def _open_backup_read(path: Path):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(descriptor)
        raise BackupRestoreError("backup archive must remain a regular file")
    return os.fdopen(descriptor, "rb")


def _verify_backup_restore(
    database_url: str,
    *,
    backup_path: Path | None = None,
) -> Path | None:
    url = _validated_url(database_url)
    pg_dump = _tool("pg_dump")
    pg_restore = _tool("pg_restore")
    temporary_database = f"{TEMP_DATABASE_PREFIX}{secrets.token_hex(8)}"
    source_environment = _postgres_environment(url)
    admin_kwargs = _connection_kwargs(url, database="postgres")
    source_kwargs = _connection_kwargs(url)
    created_database = False

    with _backup_destination(backup_path) as (archive_path, keep_archive):
        with psycopg.connect(**source_kwargs) as source:
            source_before = _public_table_counts(source)
            source_revisions = _alembic_revisions(source)
        verified = False
        try:
            with _open_backup(archive_path) as output:
                _run(
                    [
                        pg_dump,
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                        "--no-password",
                    ],
                    environment=source_environment,
                    stdout=output,
                )
            archive_mode = stat.S_IMODE(archive_path.stat().st_mode)
            if os.name != "nt" and archive_mode & 0o077:
                raise BackupRestoreError("backup archive permissions are broader than mode 0600")
            with _open_backup_read(archive_path) as archive:
                _run(
                    [pg_restore, "--list"],
                    environment=source_environment,
                    stdin=archive,
                    stdout=subprocess.DEVNULL,
                )
            with psycopg.connect(**source_kwargs) as source:
                source_after = _public_table_counts(source)
                source_revisions_after = _alembic_revisions(source)
            if source_before != source_after or source_revisions != source_revisions_after:
                raise BackupRestoreError(
                    "source rows or schema revision changed during the dump; "
                    "retry while writes and migrations are paused"
                )

            with psycopg.connect(**admin_kwargs, autocommit=True) as admin:
                _create_database(admin, temporary_database)
                created_database = True
            restore_environment = _postgres_environment(
                url,
                database=temporary_database,
            )
            with _open_backup_read(archive_path) as archive:
                _run(
                    [
                        pg_restore,
                        "--exit-on-error",
                        "--no-owner",
                        "--no-privileges",
                        "--no-password",
                        "--dbname",
                        temporary_database,
                    ],
                    environment=restore_environment,
                    stdin=archive,
                    stdout=subprocess.DEVNULL,
                )
            restored_kwargs = _connection_kwargs(url, database=temporary_database)
            with psycopg.connect(**restored_kwargs) as restored:
                restored_counts = _public_table_counts(restored)
                restored_revisions = _alembic_revisions(restored)
            if restored_counts != source_before:
                raise BackupRestoreError("restored public-table row counts do not match source")
            if restored_revisions != source_revisions:
                raise BackupRestoreError("restored Alembic revisions do not match source")
            print(
                "Backup/restore verified in an isolated temporary database: "
                f"{len(restored_counts)} tables, revisions {restored_revisions or '(none)'}"
            )
            verified = True
            return archive_path if keep_archive else None
        finally:
            if created_database:
                try:
                    with psycopg.connect(**admin_kwargs, autocommit=True) as admin:
                        _drop_database(admin, temporary_database)
                except Exception as exc:
                    raise BackupRestoreError(
                        "verification database cleanup failed; remove database "
                        f"{temporary_database!r} manually"
                    ) from exc
            if not keep_archive or not verified:
                archive_path.unlink(missing_ok=True)


def verify_backup_restore(database_url: str, *, backup_path: Path | None = None) -> Path | None:
    try:
        return _verify_backup_restore(database_url, backup_path=backup_path)
    except psycopg.Error as exc:
        primary = getattr(exc.diag, "message_primary", None)
        detail = primary or exc.__class__.__name__
        raise BackupRestoreError(
            f"PostgreSQL backup verification failed: {detail}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-path",
        type=Path,
        help="optional new absolute path to retain the verified custom-format backup",
    )
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required; keep credentials out of command-line arguments")
    try:
        retained = verify_backup_restore(database_url, backup_path=args.backup_path)
    except BackupRestoreError as exc:
        parser.error(str(exc))
    if retained is not None:
        print(f"Verified backup retained at: {retained}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
