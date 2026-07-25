import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import engine, upgrade_database


@pytest.fixture
def db_session():
    try:
        upgrade_database()
        connection = engine.connect()
    except OperationalError:
        pytest.skip("PostgreSQL is unavailable")
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
