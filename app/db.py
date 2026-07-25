import os
import shutil
import subprocess
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

LEGACY_TABLES = frozenset(
    {
        "conversation_sessions",
        "conversation_turns",
        "trade_plans",
        "trade_reflections",
    }
)


class LegacySchemaDetectedError(RuntimeError):
    pass


@dataclass(frozen=True)
class SchemaState:
    current_revision: str | None
    head_revision: str | None
    tables: frozenset[str]
    legacy_unmanaged: bool

    @property
    def current(self) -> bool:
        return self.current_revision == self.head_revision


def alembic_config(database_url: str | None = None) -> Config:
    root = Path(__file__).resolve().parent.parent
    config_path = root / "alembic.ini"
    config = Config(config_path if config_path.exists() else None)
    config.set_main_option("script_location", str(root / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url or settings.database_url)
    return config


def inspect_schema(target_engine: Engine = engine) -> SchemaState:
    config = alembic_config(str(target_engine.url))
    tables = frozenset(inspect(target_engine).get_table_names())
    with target_engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    head = ScriptDirectory.from_config(config).get_current_head()
    unmanaged = current is None and bool(tables & LEGACY_TABLES)
    return SchemaState(current, head, tables, unmanaged)


def upgrade_database(database_url: str | None = None) -> None:
    target_url = database_url or settings.database_url
    target_engine = engine if target_url == settings.database_url else create_engine(target_url)
    state = inspect_schema(target_engine)
    if state.legacy_unmanaged:
        raise LegacySchemaDetectedError(
            "unmanaged legacy tables detected; run `trading-agent db adopt-legacy "
            "--backup /absolute/path/to/backup.dump` before upgrading"
        )
    command.upgrade(alembic_config(database_url), "head")


def schema_revisions() -> tuple[str | None, str | None]:
    state = inspect_schema()
    return state.current_revision, state.head_revision


def _backup_database(backup_path: Path, database_url: str) -> None:
    if backup_path.exists():
        raise FileExistsError(f"backup path already exists: {backup_path}")
    if not backup_path.parent.is_dir():
        raise FileNotFoundError(f"backup directory does not exist: {backup_path.parent}")
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise RuntimeError("pg_dump is required for legacy adoption")
    url = make_url(database_url)
    environment = os.environ.copy()
    for name, value in {
        "PGHOST": url.host,
        "PGPORT": str(url.port) if url.port else None,
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
    }.items():
        if value is not None:
            environment[name] = value
    subprocess.run(  # noqa: S603 - executable is resolved by shutil.which; no shell is used.
        [pg_dump, "--format=custom", "--file", str(backup_path)],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    backup_path.chmod(0o600)


def adopt_legacy_database(backup_path: Path, database_url: str | None = None) -> None:
    target_url = database_url or settings.database_url
    target_engine = engine if target_url == settings.database_url else create_engine(target_url)
    state = inspect_schema(target_engine)
    if not state.legacy_unmanaged:
        raise LegacySchemaDetectedError("no unmanaged legacy schema was detected")
    unexpected = state.tables - LEGACY_TABLES
    if unexpected:
        raise LegacySchemaDetectedError(
            f"legacy adoption refused because unexpected tables exist: {sorted(unexpected)}"
        )
    _backup_database(backup_path.resolve(), target_url)

    config = alembic_config(target_url)
    with target_engine.connect() as connection, connection.begin():
        connection.execute(text("CREATE SCHEMA legacy_adoption"))
        for table in sorted(state.tables):
            connection.execute(
                text(f'ALTER TABLE public."{table}" SET SCHEMA legacy_adoption')
            )
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

        if "conversation_sessions" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.conversation_sessions "
                    "(id, name, title, created_at, updated_at) "
                    "SELECT id, name, title, created_at, updated_at "
                    "FROM legacy_adoption.conversation_sessions"
                )
            )
        if "conversation_turns" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.conversation_turns "
                    "(id, session_id, role, content, created_at) "
                    "SELECT id, session_id, role, content, created_at "
                    "FROM legacy_adoption.conversation_turns"
                )
            )
        if "trade_plans" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.trade_plans "
                    "(id, reference, instrument, venue, direction, setup_name, regime, "
                    "context_timeframe, trigger_timeframe, entry, stop, target, "
                    "account_equity, risk_percent, value_per_price_unit, risk_amount, "
                    "quantity, planned_r, thesis, invalidation, observations, "
                    "interpretations, source, status, created_at) "
                    "SELECT id, 'legacy-' || replace(id::text, '-', ''), "
                    "instrument, venue, direction, setup_name, regime, "
                    "context_timeframe, trigger_timeframe, entry, stop, target, "
                    "account_equity, risk_percent, value_per_price_unit, risk_amount, "
                    "quantity, planned_r, thesis, invalidation, observations, "
                    "interpretations, 'legacy', status, created_at "
                    "FROM legacy_adoption.trade_plans"
                )
            )
        if "trade_reflections" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.trade_reflections "
                    "(id, trade_id, exit_average, realized_pnl, realized_r, "
                    "execution_grade, rule_adherence, emotion_before, emotion_during, "
                    "emotion_after, notes, created_at) "
                    "SELECT id, trade_id, exit_average, realized_pnl, realized_r, "
                    "execution_grade, rule_adherence, emotion_before, emotion_during, "
                    "emotion_after, notes, created_at "
                    "FROM legacy_adoption.trade_reflections"
                )
            )
        for table in sorted(state.tables):
            # Names can only originate from the fixed LEGACY_TABLES allowlist above.
            legacy_count = connection.scalar(
                text(f'SELECT count(*) FROM legacy_adoption."{table}"')  # noqa: S608
            )
            migrated_count = connection.scalar(
                text(f'SELECT count(*) FROM public."{table}"')  # noqa: S608
            )
            if legacy_count != migrated_count:
                raise RuntimeError(
                    f"legacy adoption verification failed for {table}: "
                    f"{legacy_count} source rows, {migrated_count} migrated rows"
                )
        connection.execute(text("DROP SCHEMA legacy_adoption CASCADE"))


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
