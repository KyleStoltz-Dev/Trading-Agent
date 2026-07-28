import os
import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

if TYPE_CHECKING:
    from app.services.workspaces import RequestScope


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
_database_scope: ContextVar["RequestScope | None"] = ContextVar(
    "database_scope", default=None
)


@event.listens_for(Session, "after_begin")
def _reapply_tenant_scope(
    session: Session,
    transaction,
    connection,
) -> None:
    """Reapply transaction-local tenant GUCs after every commit/rollback."""
    scope = session.info.get("tenant_scope")
    if scope is None:
        return
    connection.execute(
        text("SELECT set_config('app.workspace_id', :value, true)"),
        {"value": str(scope.workspace_id)},
    )
    connection.execute(
        text("SELECT set_config('app.account_id', :value, true)"),
        {"value": str(scope.account_id)},
    )

LEGACY_TABLES = frozenset(
    {
        "conversation_sessions",
        "conversation_turns",
        "trade_plans",
        "trade_reflections",
    }
)
MIGRATION_ADVISORY_LOCK_KEY = 0x54524144454D4947
LEGACY_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_UNASSIGNED_ACCOUNT_ID = "00000000-0000-4000-8000-000000000002"


class LegacySchemaDetectedError(RuntimeError):
    pass


@contextmanager
def migration_lock(target_engine: Engine):
    """Serialize Alembic operations across every process sharing PostgreSQL."""
    if target_engine.dialect.name != "postgresql":
        yield
        return
    with target_engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"),
            {"key": MIGRATION_ADVISORY_LOCK_KEY},
        )
        connection.commit()
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": MIGRATION_ADVISORY_LOCK_KEY},
            )
            connection.commit()


@dataclass(frozen=True)
class SchemaState:
    current_revision: str | None
    head_revision: str | None
    tables: frozenset[str]
    legacy_unmanaged: bool

    @property
    def current(self) -> bool:
        return self.current_revision == self.head_revision


def alembic_config(
    database_url: str | None = None,
    *,
    configure_logger: bool = True,
) -> Config:
    root = Path(__file__).resolve().parent.parent
    config_path = root / "alembic.ini"
    config = Config(config_path if config_path.exists() else None)
    config.attributes["configure_logger"] = configure_logger
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
    with migration_lock(target_engine):
        state = inspect_schema(target_engine)
        if state.legacy_unmanaged:
            raise LegacySchemaDetectedError(
                "unmanaged legacy tables detected; run `trading-agent db adopt-legacy "
                "--backup /absolute/path/to/backup.dump` before upgrading"
            )
        command.upgrade(
            alembic_config(database_url, configure_logger=False),
            "head",
        )


def schema_revisions() -> tuple[str | None, str | None]:
    state = inspect_schema()
    return state.current_revision, state.head_revision


@contextmanager
def bind_database_scope(scope: "RequestScope"):
    """Bind one authenticated tenant scope to sessions created in this request."""
    token: Token[RequestScope | None] = _database_scope.set(scope)
    try:
        yield
    finally:
        _database_scope.reset(token)


def verify_hosted_rls(target_engine: Engine = engine) -> None:
    """Fail closed unless the runtime role is actually constrained by tenant RLS."""
    with target_engine.connect() as connection:
        role = connection.execute(
            text(
                "SELECT r.rolbypassrls, r.rolsuper "
                "FROM pg_roles r WHERE r.rolname = current_user"
            )
        ).one()
        if role.rolbypassrls or role.rolsuper:
            raise RuntimeError("hosted database role may not bypass row-level security")
        unsafe = connection.execute(
            text(
                """
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN information_schema.columns col
                    ON col.table_schema = n.nspname
                   AND col.table_name = c.relname
                 WHERE n.nspname = 'public'
                   AND (
                       col.column_name = 'workspace_id'
                       OR c.relname = 'workspaces'
                   )
                   AND c.relname <> 'api_principal_grants'
                 GROUP BY c.oid, c.relname, c.relrowsecurity, c.relowner
                HAVING NOT c.relrowsecurity
                    OR c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                    OR NOT EXISTS (
                        SELECT 1 FROM pg_policy p
                         WHERE p.polrelid = c.oid AND p.polname = 'tenant_scope'
                    )
                """
            )
        ).scalars().all()
        if unsafe:
            raise RuntimeError(
                "hosted RLS is not enforceable for the runtime role on: "
                + ", ".join(sorted(unsafe))
            )
        auth_writes = connection.execute(
            text(
                """
                SELECT table_name
                  FROM (VALUES ('api_principals'), ('api_principal_grants'))
                       AS auth_tables(table_name)
                 WHERE has_table_privilege(
                     current_user,
                     'public.' || quote_ident(table_name),
                     'INSERT,UPDATE,DELETE,TRUNCATE'
                 )
                """
            )
        ).scalars().all()
        if auth_writes:
            raise RuntimeError(
                "hosted runtime role must have read-only bootstrap auth metadata: "
                + ", ".join(sorted(auth_writes))
            )


def _backup_database(backup_path: Path, database_url: str) -> None:
    if backup_path.exists() or backup_path.is_symlink():
        raise FileExistsError(f"backup path already exists: {backup_path}")
    if not backup_path.parent.is_dir():
        raise FileNotFoundError(f"backup directory does not exist: {backup_path.parent}")
    if backup_path.parent.resolve() != backup_path.parent.absolute():
        raise ValueError("backup directory cannot contain symbolic links")
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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(backup_path, flags, 0o600)
    created = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            subprocess.run(  # noqa: S603 - resolved executable; no shell is used.
                [pg_dump, "--format=custom"],
                env=environment,
                check=True,
                stdout=output,
                stderr=subprocess.PIPE,
            )
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            current = backup_path.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                backup_path.unlink()
        except FileNotFoundError:
            pass
        raise


def adopt_legacy_database(backup_path: Path, database_url: str | None = None) -> None:
    target_url = database_url or settings.database_url
    target_engine = engine if target_url == settings.database_url else create_engine(target_url)
    with migration_lock(target_engine):
        _adopt_legacy_database_locked(backup_path, target_url, target_engine)


def _adopt_legacy_database_locked(
    backup_path: Path,
    target_url: str,
    target_engine: Engine,
) -> None:
    state = inspect_schema(target_engine)
    if not state.legacy_unmanaged:
        raise LegacySchemaDetectedError("no unmanaged legacy schema was detected")
    unexpected = state.tables - LEGACY_TABLES
    if unexpected:
        raise LegacySchemaDetectedError(
            f"legacy adoption refused because unexpected tables exist: {sorted(unexpected)}"
        )
    _backup_database(backup_path.expanduser().absolute(), target_url)

    config = alembic_config(target_url)
    with target_engine.connect() as connection, connection.begin():
        connection.execute(text("CREATE SCHEMA legacy_adoption"))
        for table in sorted(state.tables):
            connection.execute(
                text(f'ALTER TABLE public."{table}" SET SCHEMA legacy_adoption')
            )
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        legacy_scope = {
            "workspace_id": str(LEGACY_WORKSPACE_ID),
            "account_id": str(LEGACY_UNASSIGNED_ACCOUNT_ID),
        }
        connection.execute(
            text(
                "UPDATE public.trading_accounts "
                "SET active = TRUE, is_default = TRUE "
                "WHERE id = CAST(:account_id AS uuid) "
                "AND workspace_id = CAST(:workspace_id AS uuid)"
            ),
            legacy_scope,
        )

        if "conversation_sessions" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.conversation_sessions "
                    "(id, workspace_id, account_id, name, title, created_at, updated_at) "
                    "SELECT id, CAST(:workspace_id AS uuid), "
                    "CAST(:account_id AS uuid), "
                    "name, title, created_at, updated_at "
                    "FROM legacy_adoption.conversation_sessions"
                ),
                legacy_scope,
            )
        if "conversation_turns" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.conversation_turns "
                    "(id, workspace_id, account_id, session_id, role, content, "
                    "status, created_at) "
                    "SELECT id, CAST(:workspace_id AS uuid), "
                    "CAST(:account_id AS uuid), "
                    "session_id, role, content, 'complete', created_at "
                    "FROM legacy_adoption.conversation_turns"
                ),
                legacy_scope,
            )
        if "trade_plans" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.trade_plans "
                    "(id, workspace_id, account_id, reference, instrument, venue, "
                    "direction, setup_name, regime, "
                    "context_timeframe, trigger_timeframe, entry, stop, target, "
                    "account_equity, risk_percent, value_per_price_unit, risk_amount, "
                    "quantity, planned_r, thesis, invalidation, observations, "
                    "interpretations, source, status, created_at) "
                    "SELECT id, CAST(:workspace_id AS uuid), "
                    "CAST(:account_id AS uuid), "
                    "'legacy-' || replace(id::text, '-', ''), "
                    "instrument, venue, direction, setup_name, regime, "
                    "context_timeframe, trigger_timeframe, entry, stop, target, "
                    "account_equity, risk_percent, value_per_price_unit, risk_amount, "
                    "quantity, planned_r, thesis, invalidation, observations, "
                    "interpretations, 'legacy', status, created_at "
                    "FROM legacy_adoption.trade_plans"
                ),
                legacy_scope,
            )
        if "trade_reflections" in state.tables:
            connection.execute(
                text(
                    "INSERT INTO public.trade_reflections "
                    "(id, workspace_id, account_id, trade_id, exit_average, "
                    "realized_pnl, realized_r, "
                    "execution_grade, rule_adherence, emotion_before, emotion_during, "
                    "emotion_after, notes, created_at) "
                    "SELECT id, CAST(:workspace_id AS uuid), "
                    "CAST(:account_id AS uuid), "
                    "trade_id, exit_average, realized_pnl, realized_r, "
                    "execution_grade, rule_adherence, emotion_before, emotion_during, "
                    "emotion_after, notes, created_at "
                    "FROM legacy_adoption.trade_reflections"
                ),
                legacy_scope,
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
        if get_settings().deployment_mode == "hosted-multi-user":
            scope = _database_scope.get()
            if scope is None:
                raise RuntimeError("hosted database access requires an authenticated scope")
            session.info["tenant_scope"] = scope
        yield session
