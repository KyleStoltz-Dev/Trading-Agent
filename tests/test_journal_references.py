from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.journal import next_trade_reference


def test_trade_reference_allocation_takes_database_lock_and_uses_max_suffix() -> None:
    db = MagicMock()
    db.scalars.return_value = [
        "xauusd-20260725-ny-long-1",
        "xauusd-20260725-ny-long-3",
    ]
    request = SimpleNamespace(
        instrument="XAUUSD",
        market_time=datetime(2026, 7, 25, 13, 30, tzinfo=UTC),
        session_name="New York",
        direction="long",
    )

    reference = next_trade_reference(db, request)

    assert reference == "xauusd-20260725-ny-long-4"
    statement = str(db.execute.call_args.args[0])
    assert "pg_advisory_xact_lock" in statement
    assert db.execute.call_args.args[1] == {"prefix": "xauusd-20260725-ny-long"}
    db.scalars.assert_called_once()
