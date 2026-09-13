import uuid
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from rich.console import Console
from sqlalchemy import select

import app.cli as cli_module
import app.services.tradingview_import as tradingview_import_module
from app.models import ExecutionEvent, Fill, Instrument, InstrumentMapping, Trade
from app.services.broker_review import broker_trade_review
from app.services.tradingview_import import (
    TradingViewExport,
    TradingViewImportError,
    import_tradingview_export,
    is_tradingview_history_import_request,
    parse_tradingview_export,
)
from app.services.workspaces import RequestScope


def _write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_order_history_normalizes_filled_trade_lifecycle(tmp_path) -> None:
    path = _write_csv(
        tmp_path / "paper-trading-order-history-all.csv",
        "Status,Symbol,Side,Type,Quantity,Fill price,Limit price,"
        "Placing time,Closing time,Order ID\n"
        "Filled,OANDA:XAUUSD,Buy,Market,2,2500,,2026-09-08 09:29:59,2026-09-08 09:30:00,100\n"
        "Filled,OANDA:XAUUSD,Sell,Limit,1,2510,2510,2026-09-08 10:00:00,2026-09-08 10:01:00,101\n"
        "Rejected,OANDA:XAUUSD,Sell,Stop,1,,2490,2026-09-08 10:02:00,2026-09-08 10:02:01,102\n"
        "Canceled,OANDA:XAUUSD,Sell,Market,1,,,2026-09-08 10:03:00,2026-09-08 10:03:01,104\n"
        "Filled,OANDA:XAUUSD,Sell,Market,1,2520,,2026-09-08 11:00:00,2026-09-08 11:00:01,103\n",
    )

    export = parse_tradingview_export(
        path,
        default_timezone=ZoneInfo("America/New_York"),
        order_history_coverage="complete",
    )

    assert export.export_kind == "order_history"
    assert export.rows_received == 5
    assert export.rows_ignored == 0
    assert export.instruments == ("OANDA:XAUUSD",)
    assert export.realized_pnl_available is False
    assert export.history_coverage == "complete"
    assert export.lifecycle_evidence == "complete_order_history"
    assert export.started_at == datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
    assert [item.event.event_type for item in export.events] == [
        "order_fill",
        "order_fill",
        "order_rejected",
        "order_canceled",
        "order_fill",
    ]
    assert [
        item.event.trade_effects[0].effect
        for item in export.events
        if item.event.trade_effects
    ] == [
        "opened",
        "reduced",
        "closed",
    ]
    rejected, canceled = (export.events[index].event for index in (2, 3))
    assert rejected.price == Decimal("2490")
    assert rejected.external_trade_id is None
    assert rejected.trade_effects == ()
    assert canceled.price is None
    assert canceled.external_trade_id is None
    assert len(
        {
            item.event.external_trade_id
            for item in export.events
            if item.event.event_type == "order_fill"
        }
    ) == 1


def test_order_history_splits_a_position_reversal_deterministically(tmp_path) -> None:
    path = _write_csv(
        tmp_path / "History.csv",
        "Status,Symbol,Side,Quantity,Fill price,Closing time,Order ID\n"
        "Filled,NASDAQ:AAPL,Buy,1,200,2026-09-08T14:00:00Z,1\n"
        "Filled,NASDAQ:AAPL,Sell,3,201,2026-09-08T14:05:00Z,2\n",
    )

    export = parse_tradingview_export(
        path,
        default_timezone=ZoneInfo("UTC"),
        order_history_coverage="complete",
    )

    assert [item.event.quantity for item in export.events] == [
        Decimal("1"),
        Decimal("-1"),
        Decimal("-2"),
    ]
    assert [item.event.trade_effects[0].effect for item in export.events] == [
        "opened",
        "closed",
        "opened",
    ]
    assert export.events[1].event.external_id.endswith(":close")
    assert export.events[2].event.external_id.endswith(":open")


def test_partial_order_history_keeps_fills_without_inventing_trade_lifecycle(
    tmp_path,
    db_session,
    request_scope,
) -> None:
    path = _write_csv(
        tmp_path / "partial-history.csv",
        "Status,Symbol,Side,Quantity,Fill price,Closing time,Order ID\n"
        "Filled,NASDAQ:ABC,Sell,2,25,2026-09-08T14:00:00Z,close-from-before-export\n",
    )

    export = parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    assert export.history_coverage == "unverified"
    assert export.lifecycle_evidence == "execution_only"
    assert export.events[0].event.external_trade_id is None
    assert export.events[0].event.trade_effects == ()

    result = import_tradingview_export(db_session, export, scope=request_scope)

    assert result.imported_executions == 1
    assert result.imported_fills == 1
    assert result.imported_trades == 0
    assert result.history_coverage == "unverified"
    assert result.lifecycle_evidence == "execution_only"
    assert (
        db_session.scalar(
            select(Trade).where(
                Trade.workspace_id == request_scope.workspace_id,
                Trade.account_id == request_scope.account_id,
            )
        )
        is None
    )
    fill = db_session.scalar(
        select(Fill).where(
            Fill.workspace_id == request_scope.workspace_id,
            Fill.account_id == request_scope.account_id,
        )
    )
    assert fill is not None
    assert fill.trade_id is None
    execution = db_session.scalar(
        select(ExecutionEvent).where(
            ExecutionEvent.workspace_id == request_scope.workspace_id,
            ExecutionEvent.account_id == request_scope.account_id,
        )
    )
    assert execution is not None
    assert execution.trade_id is None
    assert execution.provider_metadata["lifecycle_authoritative"] is False


def test_symbol_identity_preserves_exchange_and_punctuation_without_collisions(
    tmp_path,
    db_session,
    request_scope,
) -> None:
    path = _write_csv(
        tmp_path / "symbol-history.csv",
        "Status,Symbol,Side,Quantity,Fill price,Closing time,Order ID\n"
        "Filled,NASDAQ:ABC,Buy,1,10,2026-09-08T14:00:00Z,1\n"
        "Filled,NYSE:ABC,Buy,1,10,2026-09-08T14:01:00Z,2\n"
        "Filled,NYSE:BRK.B,Buy,1,10,2026-09-08T14:02:00Z,3\n"
        "Filled,NYSE:BRK-B,Buy,1,10,2026-09-08T14:03:00Z,4\n",
    )

    export = parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    assert export.instruments == (
        "NASDAQ:ABC",
        "NYSE:ABC",
        "NYSE:BRK-B",
        "NYSE:BRK.B",
    )
    assert len({item.event.instrument for item in export.events}) == 4

    import_tradingview_export(db_session, export, scope=request_scope)
    mappings = list(
        db_session.scalars(
            select(InstrumentMapping)
            .where(
                InstrumentMapping.provider == "tradingview-paper-import",
                InstrumentMapping.external_symbol.in_(export.instruments),
            )
            .order_by(InstrumentMapping.external_symbol)
        )
    )
    assert [item.external_symbol for item in mappings] == list(export.instruments)
    assert [item.venue for item in mappings] == ["NASDAQ", "NYSE", "NYSE", "NYSE"]
    instrument_ids = {item.instrument_id for item in mappings}
    assert len(instrument_ids) == 4
    assert len(
        list(
            db_session.scalars(
                select(Instrument).where(Instrument.id.in_(instrument_ids))
            )
        )
    ) == 4


def test_account_history_preserves_exported_realized_pnl(tmp_path) -> None:
    path = _write_csv(
        tmp_path / "paper-trading-account-history.csv",
        "Symbol,Side,Quantity,Entry price,Close price,Opening time,"
        "Closing time,Realized P&L,Commission,Position ID\n"
        "OANDA:EURUSD,Long,1000,1.1000,1.1050,2026-09-08T13:00:00Z,2026-09-08T14:00:00Z,5.00,0.25,p-1\n",
    )

    export = parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    assert export.export_kind == "account_history"
    assert export.instruments == ("OANDA:EURUSD",)
    assert export.realized_pnl_available is True
    assert export.history_coverage == "unverified"
    assert export.lifecycle_evidence == "account_history"
    assert len(export.events) == 2
    opening, closing = (item.event for item in export.events)
    assert opening.realized_pnl is None
    assert closing.realized_pnl == Decimal("5.00")
    assert closing.commission == Decimal("-0.25")
    assert opening.external_trade_id == closing.external_trade_id


def test_current_trade_history_pairs_rows_and_preserves_realized_pnl(tmp_path) -> None:
    path = _write_csv(
        tmp_path / "paper-trading-trade-history.csv",
        "Symbol,Trade number,Type,Date and time,Order ID,Signal,Price,Size (qty),"
        "Size (value),Net PnL USD,Return %,Commission USD,Cumulative PnL USD,"
        "Cumulative PnL %\n"
        'OANDA:XAUUSD,21,Entry long,"Feb 28, 2024, 22:53",open-21,,2030.50,2,'
        "4061.00,15.25,0.37,0.10,15.25,0.15\n"
        'OANDA:XAUUSD,21,Exit long,"Feb 29, 2024, 01:05",close-21,,2038.25,2,'
        "4076.50,15.25,0.37,0.15,15.25,0.15\n"
        'OANDA:XAUUSD,22,Entry short,"Mar 01, 2024, 09:30",open-22,,2040,1,'
        "2040,-5,-0.25,0,-5,-0.05\n"
        'OANDA:XAUUSD,22,Exit short,"Mar 01, 2024, 10:00",close-22,,2045,1,'
        "2045,-5,-0.25,0,-5,-0.05\n",
    )

    export = parse_tradingview_export(
        path,
        default_timezone=ZoneInfo("America/New_York"),
    )

    assert export.export_kind == "trade_history"
    assert export.rows_received == 4
    assert export.rows_ignored == 0
    assert export.realized_pnl_available is True
    assert export.lifecycle_evidence == "trade_history"
    assert len(export.events) == 4
    assert [item.event.trade_effects[0].effect for item in export.events] == [
        "opened",
        "closed",
        "opened",
        "closed",
    ]
    assert [item.event.quantity for item in export.events] == [
        Decimal("2"),
        Decimal("-2"),
        Decimal("-1"),
        Decimal("1"),
    ]
    assert [item.event.realized_pnl for item in export.events] == [
        None,
        Decimal("15.25"),
        None,
        Decimal("-5"),
    ]
    assert export.events[0].event.commission == Decimal("-0.10")
    assert export.events[1].event.commission == Decimal("-0.15")
    assert export.events[0].event.occurred_at == datetime(
        2024, 2, 29, 3, 53, tzinfo=UTC
    )
    assert export.events[0].event.external_trade_id == (
        export.events[1].event.external_trade_id
    )


def test_trade_history_rejects_incomplete_or_conflicting_pairs(tmp_path) -> None:
    header = (
        "Symbol,Trade number,Type,Date and time,Order ID,Price,Size (qty),Net PnL USD\n"
    )
    incomplete = _write_csv(
        tmp_path / "incomplete.csv",
        header + 'NASDAQ:AAPL,7,Entry long,"Sep 08, 2026, 09:30",1,200,1,5\n',
    )
    with pytest.raises(TradingViewImportError, match="one entry and either one exit"):
        parse_tradingview_export(incomplete, default_timezone=ZoneInfo("UTC"))

    conflicting = _write_csv(
        tmp_path / "conflicting.csv",
        header
        + 'NASDAQ:AAPL,8,Entry long,"Sep 08, 2026, 09:30",1,200,1,5\n'
        + 'NASDAQ:AAPL,8,Exit long,"Sep 08, 2026, 10:30",2,205,1,6\n',
    )
    with pytest.raises(TradingViewImportError, match="conflicting net P&L"):
        parse_tradingview_export(conflicting, default_timezone=ZoneInfo("UTC"))


def test_trade_history_preserves_an_explicitly_open_trade(tmp_path) -> None:
    path = _write_csv(
        tmp_path / "Trade History.csv",
        "Symbol,Trade number,Type,Date and time,Order ID,Price,Size (qty),"
        "Net PnL USD,Commission USD\n"
        "NASDAQ:AAPL,9,Exit long,Open,,—,3,0,0\n"
        'NASDAQ:AAPL,9,Entry long,"Sep 08, 2026, 09:30",91,200,3,0,0\n',
    )

    export = parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    assert export.export_kind == "trade_history"
    assert export.rows_ignored == 0
    assert export.realized_pnl_available is False
    assert len(export.events) == 1
    event = export.events[0].event
    assert event.quantity == Decimal("3")
    assert event.realized_pnl is None
    assert event.trade_effects[0].effect == "opened"


def test_trade_history_opening_identity_survives_a_later_close(tmp_path) -> None:
    header = (
        "Symbol,Trade number,Type,Date and time,Order ID,Price,Size (qty),Net PnL USD\n"
    )
    entry = 'NASDAQ:AAPL,9,Entry long,"Sep 08, 2026, 09:30",91,200,3,0\n'
    open_export = parse_tradingview_export(
        _write_csv(
            tmp_path / "open.csv",
            header + "NASDAQ:AAPL,9,Exit long,Open,,—,3,0\n" + entry,
        ),
        default_timezone=ZoneInfo("UTC"),
    )
    closed_export = parse_tradingview_export(
        _write_csv(
            tmp_path / "closed.csv",
            header
            + entry
            + 'NASDAQ:AAPL,9,Exit long,"Sep 08, 2026, 10:30",92,205,3,15\n',
        ),
        default_timezone=ZoneInfo("UTC"),
    )

    assert open_export.events[0].event.external_id == (
        closed_export.events[0].event.external_id
    )
    assert open_export.events[0].event.external_trade_id == (
        closed_export.events[0].event.external_trade_id
    )


def test_trade_history_import_is_idempotent_and_updates_review(
    tmp_path,
    db_session,
    request_scope,
) -> None:
    path = _write_csv(
        tmp_path / "Trade History.csv",
        "Symbol,Trade number,Type,Date and time,Order ID,Price,Size (qty),"
        "Net PnL USD,Commission USD\n"
        'OANDA:EURUSD,3,Entry short,"Sep 08, 2026, 09:30",31,1.1050,1000,10,0\n'
        'OANDA:EURUSD,3,Exit short,"Sep 08, 2026, 10:30",32,1.1040,1000,10,0\n',
    )
    export = parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    first = import_tradingview_export(db_session, export, scope=request_scope)
    second = import_tradingview_export(db_session, export, scope=request_scope)

    assert first.imported_executions == 2
    assert first.imported_fills == 2
    assert first.imported_trades == 1
    assert second.imported_executions == 0
    assert second.duplicate_executions == 2
    review = broker_trade_review(db_session, scope=request_scope)
    assert review.trade_count == 1
    assert review.unknown_outcomes == 0
    assert review.net_pnl == Decimal("10")


def test_import_rejects_non_tradingview_columns_without_echoing_rows(tmp_path) -> None:
    path = _write_csv(tmp_path / "other.csv", "name,secret\nKyle,do-not-echo\n")

    with pytest.raises(TradingViewImportError, match="Detected columns: name, secret") as error:
        parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    assert "do-not-echo" not in str(error.value)


def test_import_sanitizes_and_bounds_header_names_in_errors(tmp_path) -> None:
    hostile_header = "\x1b[31m" + ("A" * 2_000) + "\x1b[0m"
    path = _write_csv(tmp_path / "other.csv", f"{hostile_header}\nvalue\n")

    with pytest.raises(TradingViewImportError) as error:
        parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    rendered = str(error.value)
    assert "\x1b" not in rendered
    assert len(rendered) < 750


def test_import_rejects_symbolic_links(tmp_path) -> None:
    target = _write_csv(tmp_path / "target.csv", "Status\nFilled\n")
    link = tmp_path / "history.csv"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable on this runner: {exc}")

    with pytest.raises(TradingViewImportError, match="not a link"):
        parse_tradingview_export(link, default_timezone=ZoneInfo("UTC"))


def test_export_reader_uses_a_bounded_descriptor_read(monkeypatch, tmp_path) -> None:
    path = _write_csv(tmp_path / "history.csv", "Status\nFilled\n")
    real_fdopen = tradingview_import_module.os.fdopen
    read_sizes: list[int] = []

    class TrackingReader:
        def __init__(self, descriptor: int) -> None:
            self._stream = real_fdopen(descriptor, "rb", closefd=True)

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return self._stream.read(size)

    monkeypatch.setattr(
        tradingview_import_module.os,
        "fdopen",
        lambda descriptor, *_args, **_kwargs: TrackingReader(descriptor),
    )

    with pytest.raises(TradingViewImportError, match="does not look like"):
        parse_tradingview_export(path, default_timezone=ZoneInfo("UTC"))

    assert read_sizes == [tradingview_import_module._MAX_EXPORT_BYTES + 1]


@pytest.mark.parametrize(
    "message",
    (
        "Import my TradingView trades",
        "Sync Trading View paper history",
        "Load executions from TradingView",
    ),
)
def test_explicit_tradingview_import_requests_are_recognized(message: str) -> None:
    assert is_tradingview_history_import_request(message) is True


def test_tradingview_csv_path_accepts_a_dragged_escaped_path() -> None:
    assert cli_module._tradingview_csv_path(
        r"import TradingView /Users/Kyle/Downloads/Paper\ Trading\ History.csv"
    ) == Path("/Users/Kyle/Downloads/Paper Trading History.csv")


def test_import_flow_previews_and_authorizes_before_saving(
    monkeypatch,
    tmp_path,
) -> None:
    request_scope = RequestScope(uuid.uuid4(), uuid.uuid4())
    source = tmp_path / "History.csv"
    export = TradingViewExport(
        path=source,
        export_kind="order_history",
        source_sha256="a" * 64,
        rows_received=2,
        rows_ignored=0,
        instruments=("XAUUSD",),
        started_at=datetime(2026, 9, 8, 13, tzinfo=UTC),
        ended_at=datetime(2026, 9, 8, 14, tzinfo=UTC),
        realized_pnl_available=False,
        events=(),
    )
    result = SimpleNamespace(
        imported_executions=2,
        imported_fills=2,
        imported_trades=1,
        duplicate_executions=0,
        realized_pnl_available=False,
    )
    account = SimpleNamespace(label="Paper journal")
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=StringIO(), force_terminal=False, width=80),
    )
    monkeypatch.setattr(cli_module, "_profile_timezone", Mock(return_value=ZoneInfo("UTC")))
    monkeypatch.setattr(cli_module, "parse_tradingview_export", Mock(return_value=export))
    monkeypatch.setattr(
        cli_module,
        "_configured_workspace",
        Mock(return_value=SimpleNamespace(id=request_scope.workspace_id)),
    )
    monkeypatch.setattr(cli_module, "resolve_account", Mock(return_value=account))
    authorize = Mock()
    monkeypatch.setattr(cli_module, "_authorize_direct", authorize)
    importer = Mock(return_value=result)
    monkeypatch.setattr(cli_module, "import_tradingview_export", importer)

    assert cli_module._run_tradingview_import_flow(
        Mock(),
        scope=request_scope,
        path=source,
        assume_yes=True,
    )

    authorize.assert_called_once()
    assert authorize.call_args.args[0] == "import_tradingview_history"
    assert authorize.call_args.kwargs["mutating"] is True
    assert authorize.call_args.kwargs["scope"] == request_scope
    importer.assert_called_once()


def test_tradingview_import_is_idempotent_and_uses_broker_review(
    tmp_path,
    db_session,
    request_scope,
) -> None:
    path = _write_csv(
        tmp_path / "History.csv",
        "Status,Symbol,Side,Type,Quantity,Fill price,Limit price,Stop price,Closing time,Order ID\n"
        "Filled,OANDA:XAUUSD,Buy,Market,1,2500,,,2026-09-08T13:00:00Z,1\n"
        "Rejected,OANDA:XAUUSD,Sell,Limit,1,,2490,,2026-09-08T13:30:00Z,3\n"
        "Canceled,OANDA:XAUUSD,Buy,Stop,3,,,2505,2026-09-08T13:45:00Z,4\n"
        "Filled,OANDA:XAUUSD,Sell,Market,1,2510,,,2026-09-08T14:00:00Z,2\n",
    )
    export = parse_tradingview_export(
        path,
        default_timezone=ZoneInfo("UTC"),
        order_history_coverage="complete",
    )

    first = import_tradingview_export(db_session, export, scope=request_scope)
    second = import_tradingview_export(db_session, export, scope=request_scope)

    assert first.imported_executions == 4
    assert first.imported_fills == 2
    assert first.imported_trades == 1
    assert second.imported_executions == 0
    assert second.duplicate_executions == 4
    executions = list(
        db_session.scalars(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.workspace_id == request_scope.workspace_id,
                ExecutionEvent.account_id == request_scope.account_id,
            )
            .order_by(ExecutionEvent.occurred_at)
        )
    )
    assert len(executions) == 4
    rejected_metadata = executions[1].provider_metadata
    assert rejected_metadata["order_status"] == "rejected"
    assert rejected_metadata["order_side"] == "sell"
    assert rejected_metadata["order_quantity"] == "1"
    assert rejected_metadata["intended_price"] == "2490"
    assert rejected_metadata["source_venue"] == "OANDA"
    assert rejected_metadata["lifecycle_authoritative"] is True
    canceled_metadata = executions[2].provider_metadata
    assert canceled_metadata["order_status"] == "canceled"
    assert canceled_metadata["order_side"] == "buy"
    assert canceled_metadata["order_quantity"] == "3"
    assert canceled_metadata["intended_price"] == "2505"
    assert canceled_metadata["order_type"] == "Stop"
    assert len(
        list(
            db_session.scalars(
                select(Fill).where(
                    Fill.workspace_id == request_scope.workspace_id,
                    Fill.account_id == request_scope.account_id,
                )
            )
        )
    ) == 2
    trade = db_session.scalar(
        select(Trade).where(
            Trade.workspace_id == request_scope.workspace_id,
            Trade.account_id == request_scope.account_id,
        )
    )
    assert trade is not None
    assert trade.status == "closed"
    review = broker_trade_review(db_session, scope=request_scope)
    assert review.trade_count == 1
    assert review.unknown_outcomes == 1
    assert review.net_pnl is None
