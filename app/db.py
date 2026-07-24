from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def alembic_config(database_url: str | None = None) -> Config:
    root = Path(__file__).resolve().parent.parent
    config_path = root / "alembic.ini"
    config = Config(config_path if config_path.exists() else None)
    config.set_main_option("script_location", str(root / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url or settings.database_url)
    return config


def upgrade_database(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")


def schema_revisions() -> tuple[str | None, str | None]:
    config = alembic_config()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    head = ScriptDirectory.from_config(config).get_current_head()
    return current, head


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
