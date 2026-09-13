"""Import TradingView Paper Trading CSV exports into the normalized broker ledger."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.market_data.contracts import BrokerEvent, BrokerTradeEffect
from app.models import BrokerConnection, ExecutionEvent, Fill
from app.services.broker_sync import (
    _apply_trade_effects,
    _event_hash,
    _primary_trade,
    _trade_effect_metadata,
)
from app.services.catalog import get_or_create_instrument, get_or_create_mapping
from app.services.workspaces import RequestScope, validate_scope

TRADINGVIEW_PAPER_PROVIDER = "tradingview-paper-import"
TRADINGVIEW_PAPER_VENUE = "TradingView Paper Trading"
_MAX_EXPORT_BYTES = 10 * 1024 * 1024
_MAX_EXPORT_ROWS = 50_000
_MAX_EXPORT_COLUMNS = 128
_MAX_HEADER_CHARS = 80
_HEADER_TOKEN = re.compile(r"[^a-z0-9]+")
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
)
_IMPORT_INTENT = re.compile(
    r"\b(import|sync|load|bring\s+in|add)\b.*\b(history|trades?|executions?|fills?)\b"
    r"|\b(history|trades?|executions?|fills?)\b.*\b(import|sync|load|bring\s+in|add)\b",
    re.IGNORECASE,
)

class TradingViewImportError(ValueError):
    """A safe, user-facing TradingView export validation failure."""


@dataclass(frozen=True, slots=True)
class _SourceRow:
    row_number: int
    source_symbol: str
    source_venue: str
    status: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    intended_price: Decimal | None
    order_type: str | None
    placing_time: datetime | None


@dataclass(frozen=True, slots=True)
class _ImportEvent:
    event: BrokerEvent
    source_row: _SourceRow


@dataclass(frozen=True, slots=True)
class _ParsedOrder:
    row_number: int
    symbol: str
    source_symbol: str
    source_venue: str
    order_id: str
    status: Literal["filled", "canceled", "rejected"]
    side: Literal["buy", "sell"]
    quantity: Decimal
    price: Decimal | None
    occurred_at: datetime
    order_type: str | None
    placing_time: datetime | None
    realized_pnl: Decimal | None
    commission: Decimal | None


@dataclass(frozen=True, slots=True)
class _TradeHistoryRow:
    row_number: int
    trade_number: str
    symbol: str
    source_symbol: str
    source_venue: str
    phase: Literal["entry", "exit"]
    direction: Literal["long", "short"]
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    order_id: str | None
    realized_pnl: Decimal | None
    commission: Decimal | None


@dataclass(frozen=True, slots=True)
class _OpenTradeMarker:
    row_number: int
    trade_number: str
    symbol: str
    source_symbol: str
    direction: Literal["long", "short"]
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class TradingViewExport:
    path: Path
    export_kind: Literal["order_history", "account_history", "trade_history"]
    source_sha256: str
    rows_received: int
    rows_ignored: int
    instruments: tuple[str, ...]
    started_at: datetime | None
    ended_at: datetime | None
    realized_pnl_available: bool
    history_coverage: Literal["unverified", "complete"] = "unverified"
    lifecycle_evidence: Literal[
        "execution_only", "complete_order_history", "account_history", "trade_history"
    ] = "execution_only"
    events: tuple[_ImportEvent, ...] = field(default_factory=tuple, repr=False)


@dataclass(frozen=True, slots=True)
class TradingViewImportResult:
    export_kind: str
    imported_executions: int
    imported_fills: int
    imported_trades: int
    duplicate_executions: int
    rows_ignored: int
    instruments: tuple[str, ...]
    realized_pnl_available: bool
    history_coverage: Literal["unverified", "complete"]
    lifecycle_evidence: Literal[
        "execution_only", "complete_order_history", "account_history", "trade_history"
    ]


def is_tradingview_history_import_request(message: str) -> bool:
    """Recognize an explicit request to import TradingView trading records."""
    normalized = " ".join(message.casefold().split())
    mentions_tradingview = "tradingview" in normalized or "trading view" in normalized
    return mentions_tradingview and _IMPORT_INTENT.search(normalized) is not None


def _sanitized_text(value: str, *, limit: int) -> str:
    """Remove terminal controls and bound text retained from an untrusted CSV."""
    without_ansi = _ANSI_ESCAPE.sub("", value)
    printable = "".join(
        character if character.isprintable() else " " for character in without_ansi
    )
    return " ".join(printable.split())[:limit]


def _header_token(value: str) -> str:
    return _HEADER_TOKEN.sub("", value.casefold().replace("&", "and"))


def _column(
    headers: dict[str, str],
    *aliases: str,
    required: bool = True,
) -> str | None:
    for alias in aliases:
        found = headers.get(_header_token(alias))
        if found is not None:
            return found
    if required:
        raise TradingViewImportError(
            f"The TradingView export is missing the “{aliases[0]}” column."
        )
    return None


def _cell(row: dict[str, str | None], column: str | None) -> str:
    if column is None:
        return ""
    return str(row.get(column) or "").strip()


def _decimal(value: str, *, field_name: str, row_number: int) -> Decimal:
    candidate = value.strip().replace("\u00a0", "").replace(" ", "")
    negative = candidate.startswith("(") and candidate.endswith(")")
    if negative:
        candidate = candidate[1:-1]
    candidate = re.sub(r"(?i)(USD|EUR|GBP|JPY|CAD|AUD|CHF|NZD)$", "", candidate)
    candidate = candidate.strip("$€£¥")
    if "," in candidate and "." in candidate:
        if candidate.rfind(",") > candidate.rfind("."):
            candidate = candidate.replace(".", "").replace(",", ".")
        else:
            candidate = candidate.replace(",", "")
    elif "," in candidate:
        tail = candidate.rsplit(",", 1)[1]
        candidate = candidate.replace(",", "" if len(tail) == 3 else ".")
    if negative:
        candidate = f"-{candidate}"
    try:
        number = Decimal(candidate)
    except InvalidOperation as exc:
        raise TradingViewImportError(
            f"Row {row_number} has an invalid {field_name}."
        ) from exc
    if not number.is_finite():
        raise TradingViewImportError(
            f"Row {row_number} has a non-finite {field_name}."
        )
    return number


def _timestamp(
    value: str,
    *,
    field_name: str,
    row_number: int,
    default_timezone: ZoneInfo,
) -> datetime:
    candidate = value.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        for pattern in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
            "%b %d, %Y, %H:%M:%S",
            "%b %d, %Y, %H:%M",
            "%b %d, %Y %H:%M:%S",
            "%b %d, %Y %H:%M",
        ):
            try:
                parsed = datetime.strptime(candidate, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        raise TradingViewImportError(
            f"Row {row_number} has an unsupported {field_name}. "
            "Use the unedited TradingView CSV and choose its display timezone."
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(UTC)


def _symbol(value: str, *, row_number: int) -> tuple[str, str, str]:
    source_symbol = _sanitized_text(value.strip(), limit=81)
    if not source_symbol or len(source_symbol) > 80:
        raise TradingViewImportError(f"Row {row_number} has an invalid symbol.")
    normalized_source = source_symbol.upper()
    plain_identity = "".join(
        character for character in normalized_source if character.isalnum()
    )
    if not plain_identity:
        raise TradingViewImportError(f"Row {row_number} has an invalid symbol.")
    if plain_identity == normalized_source and len(plain_identity) <= 40:
        symbol_identity = plain_identity
    else:
        # The catalog canonicalizer removes punctuation. A digest prevents collisions
        # such as NASDAQ:ABC vs NYSE:ABC and BRK.B vs BRK-B after that step.
        digest = hashlib.sha256(normalized_source.encode()).hexdigest()[:32].upper()
        symbol_identity = f"{plain_identity[:7]}_{digest}"
    venue, separator, _ = source_symbol.rpartition(":")
    source_venue = (
        _sanitized_text(venue, limit=80)
        if separator and venue.strip()
        else TRADINGVIEW_PAPER_VENUE
    )
    return symbol_identity, source_symbol, source_venue


def _side(value: str, *, row_number: int) -> Literal["buy", "sell"]:
    normalized = value.strip().casefold()
    if normalized in {"buy", "long"}:
        return "buy"
    if normalized in {"sell", "short"}:
        return "sell"
    raise TradingViewImportError(
        f"Row {row_number} has an unsupported side; expected Buy/Sell or Long/Short."
    )


def _stable_key(*values: object) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _safe_export_path(path: Path) -> tuple[Path, bytes]:
    expanded = path.expanduser()
    if expanded.suffix.casefold() != ".csv":
        raise TradingViewImportError("Choose the CSV exported by TradingView.")
    try:
        path_details = expanded.lstat()
    except OSError as exc:
        raise TradingViewImportError("That TradingView CSV could not be opened.") from exc
    if stat.S_ISLNK(path_details.st_mode) or not stat.S_ISREG(path_details.st_mode):
        raise TradingViewImportError("Choose a regular CSV file, not a link or folder.")
    if path_details.st_size > _MAX_EXPORT_BYTES:
        raise TradingViewImportError("That TradingView CSV is larger than the 10 MB limit.")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    flags |= no_follow
    descriptor: int | None = None
    try:
        descriptor = os.open(expanded, flags)
        opened_details = os.fstat(descriptor)
        if not stat.S_ISREG(opened_details.st_mode):
            raise TradingViewImportError(
                "Choose a regular CSV file, not a link or folder."
            )
        path_identity = (path_details.st_dev, path_details.st_ino)
        opened_identity = (opened_details.st_dev, opened_details.st_ino)
        if path_identity != opened_identity:
            raise TradingViewImportError(
                "That TradingView CSV changed while it was being opened. Try again."
            )
        if opened_details.st_size > _MAX_EXPORT_BYTES:
            raise TradingViewImportError(
                "That TradingView CSV is larger than the 10 MB limit."
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            payload = stream.read(_MAX_EXPORT_BYTES + 1)
        if len(payload) > _MAX_EXPORT_BYTES:
            raise TradingViewImportError(
                "That TradingView CSV is larger than the 10 MB limit."
            )
    except TradingViewImportError:
        raise
    except OSError as exc:
        raise TradingViewImportError("That TradingView CSV could not be read.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if b"\x00" in payload:
        raise TradingViewImportError("That file is not a plain-text TradingView CSV.")
    return expanded.absolute(), payload


def _read_rows(payload: bytes) -> tuple[list[str], list[dict[str, str | None]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TradingViewImportError("TradingView CSV files must use UTF-8 text.") from exc
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise TradingViewImportError("That CSV has no header row.")
    raw_headers = [str(item) for item in reader.fieldnames]
    if len(raw_headers) > _MAX_EXPORT_COLUMNS:
        raise TradingViewImportError("That CSV has too many columns.")
    headers = [
        _sanitized_text(item.strip(), limit=_MAX_HEADER_CHARS) for item in raw_headers
    ]
    if len({_header_token(item) for item in headers}) != len(headers):
        raise TradingViewImportError("That CSV contains duplicate column names.")
    rows: list[dict[str, str | None]] = []
    for row_number, row in enumerate(reader, start=2):
        if row_number > _MAX_EXPORT_ROWS + 1:
            raise TradingViewImportError("That TradingView CSV has more than 50,000 rows.")
        if None in row:
            raise TradingViewImportError(
                f"Row {row_number} has more values than the CSV header."
            )
        rows.append(
            {
                safe_header: row.get(raw_header)
                for raw_header, safe_header in zip(raw_headers, headers, strict=True)
            }
        )
    return headers, rows


def _order_history_events(
    rows: list[dict[str, str | None]],
    headers: dict[str, str],
    *,
    default_timezone: ZoneInfo,
    authoritative_lifecycle: bool,
) -> tuple[tuple[_ImportEvent, ...], int, bool]:
    status_col = _column(headers, "Status")
    symbol_col = _column(headers, "Symbol")
    side_col = _column(headers, "Side")
    quantity_col = _column(headers, "Quantity", "Qty")
    fill_price_col = _column(headers, "Fill price", "Fill Price")
    limit_price_col = _column(headers, "Limit price", "Limit Price", required=False)
    stop_price_col = _column(headers, "Stop price", "Stop Price", required=False)
    closing_time_col = _column(headers, "Closing time", "Closing Time")
    order_id_col = _column(headers, "Order ID", "Order Id")
    order_type_col = _column(headers, "Type", "Order Type", required=False)
    placing_time_col = _column(headers, "Placing time", "Placing Time", required=False)
    pnl_col = _column(
        headers,
        "Realized P&L",
        "Realized PnL",
        "Realized profit",
        required=False,
    )
    commission_col = _column(headers, "Commission", required=False)

    parsed: list[_ParsedOrder] = []
    ignored = 0
    for row_number, row in enumerate(rows, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            ignored += 1
            continue
        raw_status = _sanitized_text(_cell(row, status_col), limit=24).casefold()
        status_aliases = {
            "filled": "filled",
            "canceled": "canceled",
            "cancelled": "canceled",
            "rejected": "rejected",
        }
        try:
            status = status_aliases[raw_status]
        except KeyError as exc:
            raise TradingViewImportError(
                f"Row {row_number} has an unsupported order status."
            ) from exc
        symbol, source_symbol, source_venue = _symbol(
            _cell(row, symbol_col), row_number=row_number
        )
        side = _side(_cell(row, side_col), row_number=row_number)
        quantity = _decimal(
            _cell(row, quantity_col), field_name="quantity", row_number=row_number
        )
        if quantity <= 0:
            raise TradingViewImportError(f"Row {row_number} quantity must be positive.")
        price_text = _cell(row, fill_price_col)
        if status != "filled":
            price_text = (
                _cell(row, limit_price_col)
                or _cell(row, stop_price_col)
                or price_text
            )
        if status == "filled" and not price_text:
            raise TradingViewImportError(
                f"Row {row_number} is filled but has no fill price."
            )
        price = (
            _decimal(price_text, field_name="price", row_number=row_number)
            if price_text
            else None
        )
        if price is not None and price <= 0:
            raise TradingViewImportError(f"Row {row_number} fill price must be positive.")
        occurred_at = _timestamp(
            _cell(row, closing_time_col),
            field_name="closing time",
            row_number=row_number,
            default_timezone=default_timezone,
        )
        order_id = _sanitized_text(_cell(row, order_id_col), limit=161)
        if not order_id or len(order_id) > 160:
            raise TradingViewImportError(f"Row {row_number} has an invalid order ID.")
        placing_time = None
        if _cell(row, placing_time_col):
            placing_time = _timestamp(
                _cell(row, placing_time_col),
                field_name="placing time",
                row_number=row_number,
                default_timezone=default_timezone,
            )
        realized_pnl = (
            _decimal(_cell(row, pnl_col), field_name="realized P&L", row_number=row_number)
            if _cell(row, pnl_col)
            else None
        )
        commission = (
            -abs(
                _decimal(
                    _cell(row, commission_col),
                    field_name="commission",
                    row_number=row_number,
                )
            )
            if _cell(row, commission_col)
            else None
        )
        order_type = _sanitized_text(_cell(row, order_type_col), limit=80) or None
        parsed.append(
            _ParsedOrder(
                row_number=row_number,
                symbol=symbol,
                source_symbol=source_symbol,
                source_venue=source_venue,
                order_id=order_id,
                status=status,  # type: ignore[arg-type]
                side=side,
                quantity=quantity,
                price=price,
                occurred_at=occurred_at,
                order_type=order_type,
                placing_time=placing_time,
                realized_pnl=realized_pnl,
                commission=commission,
            )
        )

    parsed.sort(key=lambda item: (item.occurred_at, item.row_number))
    positions: dict[str, tuple[Decimal, str | None]] = {}
    events: list[_ImportEvent] = []
    for order in parsed:
        signed_quantity = order.quantity if order.side == "buy" else -order.quantity
        net, active_trade_id = positions.get(order.symbol, (Decimal("0"), None))
        order_key = _stable_key(order.source_symbol, order.order_id)
        source_row = _SourceRow(
            row_number=order.row_number,
            source_symbol=order.source_symbol,
            source_venue=order.source_venue,
            status=order.status,
            side=order.side,
            quantity=order.quantity,
            intended_price=order.price,
            order_type=order.order_type,
            placing_time=order.placing_time,
        )

        if order.status != "filled":
            events.append(
                _ImportEvent(
                    event=BrokerEvent(
                        external_id=f"tv-paper:{order_key}:{order.status}",
                        event_type=f"order_{order.status}",
                        occurred_at=order.occurred_at,
                        instrument=order.symbol,
                        external_order_id=order.order_id,
                        external_trade_id=None,
                        quantity=signed_quantity,
                        price=order.price,
                        realized_pnl=None,
                        source=TRADINGVIEW_PAPER_PROVIDER,
                        infer_trade_open=False,
                    ),
                    source_row=source_row,
                )
            )
            continue

        if order.price is None:
            raise TradingViewImportError(
                f"Row {order.row_number} is filled but has no fill price."
            )

        if not authoritative_lifecycle:
            events.append(
                _ImportEvent(
                    event=BrokerEvent(
                        external_id=f"tv-paper:{order_key}:fill",
                        event_type="order_fill",
                        occurred_at=order.occurred_at,
                        instrument=order.symbol,
                        external_order_id=order.order_id,
                        external_trade_id=None,
                        quantity=signed_quantity,
                        price=order.price,
                        realized_pnl=order.realized_pnl,
                        commission=order.commission,
                        source=TRADINGVIEW_PAPER_PROVIDER,
                        infer_trade_open=False,
                    ),
                    source_row=source_row,
                )
            )
            continue

        def append_event(
            *,
            suffix: str,
            event_quantity: Decimal,
            trade_id: str,
            effect: Literal["opened", "reduced", "closed"],
            event_pnl: Decimal | None,
            event_commission: Decimal | None,
            event_order_key: str = order_key,
            event_occurred_at: datetime = order.occurred_at,
            event_symbol: str = order.symbol,
            event_price: Decimal = order.price,
            event_source_row: _SourceRow = source_row,
            event_order_id: str = order.order_id,
        ) -> None:
            events.append(
                _ImportEvent(
                    event=BrokerEvent(
                        external_id=f"tv-paper:{event_order_key}:{suffix}",
                        event_type="order_fill",
                        occurred_at=event_occurred_at,
                        instrument=event_symbol,
                        external_order_id=event_order_id,
                        external_trade_id=trade_id,
                        quantity=event_quantity,
                        price=event_price,
                        realized_pnl=event_pnl,
                        commission=event_commission,
                        source=TRADINGVIEW_PAPER_PROVIDER,
                        trade_effects=(
                            BrokerTradeEffect(
                                external_trade_id=trade_id,
                                effect=effect,
                                quantity=event_quantity,
                                realized_pnl=event_pnl,
                            ),
                        ),
                        infer_trade_open=False,
                    ),
                    source_row=event_source_row,
                )
            )

        if net == 0 or (net > 0) == (signed_quantity > 0):
            trade_id = active_trade_id or f"tv-paper-trade:{order_key}"
            append_event(
                suffix="fill",
                event_quantity=signed_quantity,
                trade_id=trade_id,
                effect="opened",
                event_pnl=order.realized_pnl,
                event_commission=order.commission,
            )
            positions[order.symbol] = (net + signed_quantity, trade_id)
            continue

        if active_trade_id is None:
            raise TradingViewImportError(
                f"Row {order.row_number} cannot be matched to a position in this export."
            )
        close_quantity = min(abs(net), abs(signed_quantity))
        close_signed = -close_quantity if net > 0 else close_quantity
        remaining = signed_quantity - close_signed
        effect: Literal["reduced", "closed"] = (
            "closed" if abs(signed_quantity) >= abs(net) else "reduced"
        )
        append_event(
            suffix="close" if remaining else "fill",
            event_quantity=close_signed,
            trade_id=active_trade_id,
            effect=effect,
            event_pnl=order.realized_pnl,
            event_commission=order.commission if remaining == 0 else None,
        )
        if remaining:
            reversal_trade_id = f"tv-paper-trade:{order_key}:reversal"
            append_event(
                suffix="open",
                event_quantity=remaining,
                trade_id=reversal_trade_id,
                effect="opened",
                event_pnl=None,
                event_commission=order.commission,
            )
            positions[order.symbol] = (remaining, reversal_trade_id)
        else:
            updated_net = net + signed_quantity
            positions[order.symbol] = (
                updated_net,
                None if updated_net == 0 else active_trade_id,
            )
    return tuple(events), ignored, any(item.realized_pnl is not None for item in parsed)


def _account_history_events(
    rows: list[dict[str, str | None]],
    headers: dict[str, str],
    *,
    default_timezone: ZoneInfo,
) -> tuple[tuple[_ImportEvent, ...], int, bool]:
    symbol_col = _column(headers, "Symbol")
    side_col = _column(headers, "Side", "Direction")
    quantity_col = _column(headers, "Quantity", "Qty")
    entry_col = _column(
        headers,
        "Entry price",
        "Entry Price",
        "Avg entry price",
        "Avg Fill Price",
    )
    exit_col = _column(
        headers,
        "Close price",
        "Close Price",
        "Exit price",
        "Exit Price",
    )
    opened_col = _column(
        headers,
        "Opening time",
        "Open time",
        "Entry time",
        "Placing time",
    )
    closed_col = _column(
        headers,
        "Closing time",
        "Close time",
        "Exit time",
    )
    pnl_col = _column(
        headers,
        "Realized P&L",
        "Realized PnL",
        "Realized profit",
        "Profit",
    )
    id_col = _column(
        headers,
        "Position ID",
        "Trade ID",
        "Order ID",
        required=False,
    )
    commission_col = _column(headers, "Commission", required=False)

    events: list[_ImportEvent] = []
    ignored = 0
    for row_number, row in enumerate(rows, start=2):
        if not any(_cell(row, column) for column in (symbol_col, entry_col, exit_col)):
            ignored += 1
            continue
        symbol, source_symbol, source_venue = _symbol(
            _cell(row, symbol_col), row_number=row_number
        )
        opening_side = _side(_cell(row, side_col), row_number=row_number)
        quantity = _decimal(
            _cell(row, quantity_col), field_name="quantity", row_number=row_number
        )
        entry_price = _decimal(
            _cell(row, entry_col), field_name="entry price", row_number=row_number
        )
        exit_price = _decimal(
            _cell(row, exit_col), field_name="close price", row_number=row_number
        )
        if quantity <= 0 or entry_price <= 0 or exit_price <= 0:
            raise TradingViewImportError(
                f"Row {row_number} quantity and prices must be positive."
            )
        opened_at = _timestamp(
            _cell(row, opened_col),
            field_name="opening time",
            row_number=row_number,
            default_timezone=default_timezone,
        )
        closed_at = _timestamp(
            _cell(row, closed_col),
            field_name="closing time",
            row_number=row_number,
            default_timezone=default_timezone,
        )
        if closed_at < opened_at:
            raise TradingViewImportError(
                f"Row {row_number} closes before its opening time."
            )
        realized_pnl = _decimal(
            _cell(row, pnl_col), field_name="realized P&L", row_number=row_number
        )
        commission = (
            -abs(
                _decimal(
                    _cell(row, commission_col),
                    field_name="commission",
                    row_number=row_number,
                )
            )
            if _cell(row, commission_col)
            else None
        )
        source_id = _sanitized_text(_cell(row, id_col), limit=161)
        if len(source_id) > 160:
            raise TradingViewImportError(f"Row {row_number} has an invalid trade ID.")
        trade_key = _stable_key(
            source_id,
            source_symbol,
            opening_side,
            quantity,
            opened_at,
            closed_at,
        )
        trade_id = f"tv-paper-trade:{trade_key}"
        source_row = _SourceRow(
            row_number=row_number,
            source_symbol=source_symbol,
            source_venue=source_venue,
            status="closed",
            side=opening_side,
            quantity=quantity,
            intended_price=entry_price,
            order_type=None,
            placing_time=opened_at,
        )
        signed_open = quantity if opening_side == "buy" else -quantity
        for suffix, occurred_at, signed, price, effect, pnl, cost in (
            ("open", opened_at, signed_open, entry_price, "opened", None, None),
            (
                "close",
                closed_at,
                -signed_open,
                exit_price,
                "closed",
                realized_pnl,
                commission,
            ),
        ):
            events.append(
                _ImportEvent(
                    event=BrokerEvent(
                        external_id=f"tv-paper:{trade_key}:{suffix}",
                        event_type="order_fill",
                        occurred_at=occurred_at,
                        instrument=symbol,
                        external_order_id=source_id[:160] or trade_key,
                        external_trade_id=trade_id,
                        quantity=signed,
                        price=price,
                        realized_pnl=pnl,
                        commission=cost,
                        source=TRADINGVIEW_PAPER_PROVIDER,
                        trade_effects=(
                            BrokerTradeEffect(
                                external_trade_id=trade_id,
                                effect=effect,  # type: ignore[arg-type]
                                quantity=signed,
                                realized_pnl=pnl,
                            ),
                        ),
                        infer_trade_open=False,
                    ),
                    source_row=source_row,
                )
            )
    events.sort(key=lambda item: (item.event.occurred_at, item.event.external_id))
    return tuple(events), ignored, True


def _trade_history_events(
    rows: list[dict[str, str | None]],
    headers: dict[str, str],
    *,
    default_timezone: ZoneInfo,
) -> tuple[tuple[_ImportEvent, ...], int, bool]:
    """Normalize TradingView's paired-row Paper Trading Trade History export."""
    symbol_col = _column(headers, "Symbol")
    trade_number_col = _column(headers, "Trade number", "Trade #")
    type_col = _column(headers, "Type")
    occurred_at_col = _column(headers, "Date and time", "Date & time")
    price_col = _column(headers, "Price")
    quantity_col = _column(headers, "Size (qty)", "Quantity", "Qty")
    pnl_col = _column(headers, "Net PnL USD", "Net P&L USD", "Net PnL")
    order_id_col = _column(headers, "Order ID", "Order Id", required=False)
    commission_col = _column(
        headers,
        "Commission USD",
        "Commission",
        required=False,
    )

    parsed: list[_TradeHistoryRow] = []
    open_markers: dict[str, list[_OpenTradeMarker]] = {}
    ignored = 0
    for row_number, row in enumerate(rows, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            ignored += 1
            continue
        trade_number = _sanitized_text(_cell(row, trade_number_col), limit=161)
        if not trade_number or len(trade_number) > 160:
            raise TradingViewImportError(
                f"Row {row_number} has an invalid trade number."
            )
        type_value = _sanitized_text(_cell(row, type_col), limit=40).casefold()
        type_match = re.fullmatch(r"(entry|exit)\s+(long|short)", type_value)
        if type_match is None:
            raise TradingViewImportError(
                f"Row {row_number} has an unsupported Trade History type."
            )
        phase, direction = type_match.groups()
        symbol, source_symbol, source_venue = _symbol(
            _cell(row, symbol_col), row_number=row_number
        )
        quantity = _decimal(
            _cell(row, quantity_col), field_name="quantity", row_number=row_number
        )
        if quantity <= 0:
            raise TradingViewImportError(
                f"Row {row_number} quantity must be positive."
            )
        price_text = _cell(row, price_col)
        occurred_at_text = _cell(row, occurred_at_col)
        if (
            phase == "exit"
            and occurred_at_text.casefold() == "open"
            and price_text in {"", "-", "—"}
        ):
            marker = _OpenTradeMarker(
                row_number=row_number,
                trade_number=trade_number,
                symbol=symbol,
                source_symbol=source_symbol,
                direction=direction,  # type: ignore[arg-type]
                quantity=quantity,
            )
            open_markers.setdefault(trade_number, []).append(marker)
            continue
        price = _decimal(price_text, field_name="price", row_number=row_number)
        if price <= 0:
            raise TradingViewImportError(
                f"Row {row_number} price must be positive."
            )
        occurred_at = _timestamp(
            occurred_at_text,
            field_name="date and time",
            row_number=row_number,
            default_timezone=default_timezone,
        )
        order_id = _sanitized_text(_cell(row, order_id_col), limit=161) or None
        if order_id is not None and len(order_id) > 160:
            raise TradingViewImportError(f"Row {row_number} has an invalid order ID.")
        realized_pnl = (
            _decimal(
                _cell(row, pnl_col),
                field_name="net P&L",
                row_number=row_number,
            )
            if _cell(row, pnl_col)
            else None
        )
        commission = (
            -abs(
                _decimal(
                    _cell(row, commission_col),
                    field_name="commission",
                    row_number=row_number,
                )
            )
            if _cell(row, commission_col)
            else None
        )
        parsed.append(
            _TradeHistoryRow(
                row_number=row_number,
                trade_number=trade_number,
                symbol=symbol,
                source_symbol=source_symbol,
                source_venue=source_venue,
                phase=phase,  # type: ignore[arg-type]
                direction=direction,  # type: ignore[arg-type]
                quantity=quantity,
                price=price,
                occurred_at=occurred_at,
                order_id=order_id,
                realized_pnl=realized_pnl,
                commission=commission,
            )
        )

    groups: dict[str, list[_TradeHistoryRow]] = {}
    for item in parsed:
        groups.setdefault(item.trade_number, []).append(item)

    events: list[_ImportEvent] = []
    pnl_available = False
    all_trade_numbers = set(groups) | set(open_markers)
    for trade_number in all_trade_numbers:
        group = groups.get(trade_number, [])
        markers = open_markers.get(trade_number, [])
        entries = [item for item in group if item.phase == "entry"]
        exits = [item for item in group if item.phase == "exit"]
        is_complete = (
            len(group) == 2
            and len(entries) == 1
            and len(exits) == 1
            and not markers
        )
        is_open = (
            len(group) == 1
            and len(entries) == 1
            and not exits
            and len(markers) == 1
        )
        if not is_complete and not is_open:
            raise TradingViewImportError(
                f"Trade {trade_number} must contain one entry and either one exit "
                "or TradingView's explicit open-trade marker."
            )
        entry = entries[0]
        counterpart = exits[0] if is_complete else markers[0]
        if (
            entry.symbol != counterpart.symbol
            or entry.source_symbol != counterpart.source_symbol
            or entry.direction != counterpart.direction
            or entry.quantity != counterpart.quantity
        ):
            raise TradingViewImportError(
                f"Trade {trade_number} has conflicting entry and exit details."
            )
        exit_row = exits[0] if is_complete else None
        if exit_row is not None and exit_row.occurred_at < entry.occurred_at:
            raise TradingViewImportError(
                f"Trade {trade_number} exits before its entry time."
            )
        entry_pnl = entry.realized_pnl if is_complete else None
        exit_pnl = exit_row.realized_pnl if exit_row is not None else None
        if (
            exit_pnl is not None
            and entry_pnl is not None
            and entry_pnl not in {Decimal("0"), exit_pnl}
        ):
            raise TradingViewImportError(
                f"Trade {trade_number} has conflicting net P&L values."
            )
        realized_pnl = exit_pnl if exit_pnl is not None else entry_pnl
        pnl_available = pnl_available or realized_pnl is not None
        opening_side: Literal["buy", "sell"] = (
            "buy" if entry.direction == "long" else "sell"
        )
        closing_side: Literal["buy", "sell"] = (
            "sell" if opening_side == "buy" else "buy"
        )
        signed_open = entry.quantity if opening_side == "buy" else -entry.quantity
        trade_key = _stable_key(
            trade_number,
            entry.source_symbol,
            entry.direction,
            entry.quantity,
            entry.occurred_at,
        )
        trade_id = f"tv-paper-trade:{trade_key}"

        event_rows: list[
            tuple[
                str,
                _TradeHistoryRow,
                Literal["buy", "sell"],
                Decimal,
                Literal["opened", "closed"],
                Decimal | None,
            ]
        ] = [("open", entry, opening_side, signed_open, "opened", None)]
        if exit_row is not None:
            event_rows.append(
                (
                    "close",
                    exit_row,
                    closing_side,
                    -signed_open,
                    "closed",
                    realized_pnl,
                )
            )
        for suffix, item, side, signed, effect, pnl in event_rows:
            external_order_id = item.order_id or f"{trade_key}:{suffix}"
            source_row = _SourceRow(
                row_number=item.row_number,
                source_symbol=item.source_symbol,
                source_venue=item.source_venue,
                status="closed" if suffix == "close" else "filled",
                side=side,
                quantity=item.quantity,
                intended_price=item.price,
                order_type=f"Trade History {item.phase}",
                placing_time=item.occurred_at,
            )
            events.append(
                _ImportEvent(
                    event=BrokerEvent(
                        external_id=f"tv-paper:{trade_key}:{suffix}",
                        event_type="order_fill",
                        occurred_at=item.occurred_at,
                        instrument=item.symbol,
                        external_order_id=external_order_id,
                        external_trade_id=trade_id,
                        quantity=signed,
                        price=item.price,
                        realized_pnl=pnl,
                        commission=item.commission,
                        source=TRADINGVIEW_PAPER_PROVIDER,
                        trade_effects=(
                            BrokerTradeEffect(
                                external_trade_id=trade_id,
                                effect=effect,  # type: ignore[arg-type]
                                quantity=signed,
                                realized_pnl=pnl,
                            ),
                        ),
                        infer_trade_open=False,
                    ),
                    source_row=source_row,
                )
            )
    events.sort(key=lambda item: (item.event.occurred_at, item.event.external_id))
    return tuple(events), ignored, pnl_available


def parse_tradingview_export(
    path: Path,
    *,
    default_timezone: ZoneInfo,
    order_history_coverage: Literal["unverified", "complete"] = "unverified",
) -> TradingViewExport:
    """Validate and normalize one unedited TradingView Paper Trading CSV.

    Order History does not prove the position state before its first row. Its fills
    therefore remain execution-only unless the caller has separately established
    that the export contains the complete account history. Account History rows and
    paired Trade History rows explicitly carry their own open/close lifecycle and are
    authoritative per trade.
    """
    if order_history_coverage not in {"unverified", "complete"}:
        raise TradingViewImportError(
            "Order History coverage must be unverified or complete."
        )
    resolved, payload = _safe_export_path(path)
    header_names, rows = _read_rows(payload)
    headers = {_header_token(name): name for name in header_names}
    tokens = set(headers)
    try:
        if {"status", "fillprice", "closingtime", "orderid"} <= tokens:
            export_kind: Literal[
                "order_history", "account_history", "trade_history"
            ] = "order_history"
            events, ignored, pnl_available = _order_history_events(
                rows,
                headers,
                default_timezone=default_timezone,
                authoritative_lifecycle=order_history_coverage == "complete",
            )
            history_coverage = order_history_coverage
            lifecycle_evidence: Literal[
                "execution_only",
                "complete_order_history",
                "account_history",
                "trade_history",
            ] = (
                "complete_order_history"
                if order_history_coverage == "complete"
                else "execution_only"
            )
        elif {
            "symbol",
            "tradenumber",
            "type",
            "dateandtime",
            "price",
            "sizeqty",
            "netpnlusd",
        } <= tokens:
            export_kind = "trade_history"
            events, ignored, pnl_available = _trade_history_events(
                rows,
                headers,
                default_timezone=default_timezone,
            )
            history_coverage = "unverified"
            lifecycle_evidence = "trade_history"
        elif (
            {"symbol", "side", "closingtime"} <= tokens
            and any(item in tokens for item in ("entryprice", "avgentryprice", "avgfillprice"))
            and any(item in tokens for item in ("closeprice", "exitprice"))
            and any(
                item in tokens
                for item in (
                    "realizedpandl",
                    "realizedpnl",
                    "realizedprofit",
                    "profit",
                )
            )
        ):
            export_kind = "account_history"
            events, ignored, pnl_available = _account_history_events(
                rows,
                headers,
                default_timezone=default_timezone,
            )
            history_coverage = "unverified"
            lifecycle_evidence = "account_history"
        else:
            shown = ", ".join(header_names[:12])[:600]
            raise TradingViewImportError(
                "This does not look like TradingView Paper Trading Order History, "
                f"Trade History, or Account History. Detected columns: {shown or 'none'}."
            )
    except TradingViewImportError:
        raise
    if not events:
        raise TradingViewImportError(
            "No filled executions or closed trades were found in that TradingView export."
        )
    timestamps = [item.event.occurred_at for item in events]
    instruments = tuple(sorted({item.source_row.source_symbol for item in events}))
    return TradingViewExport(
        path=resolved,
        export_kind=export_kind,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        rows_received=len(rows),
        rows_ignored=ignored,
        instruments=instruments,
        started_at=min(timestamps, default=None),
        ended_at=max(timestamps, default=None),
        realized_pnl_available=pnl_available,
        history_coverage=history_coverage,
        lifecycle_evidence=lifecycle_evidence,
        events=events,
    )


def import_tradingview_export(
    db: Session,
    export: TradingViewExport,
    *,
    scope: RequestScope,
) -> TradingViewImportResult:
    """Idempotently store normalized TradingView executions in one account scope."""
    validate_scope(db, scope)
    connection = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.workspace_id == scope.workspace_id,
            BrokerConnection.account_id == scope.account_id,
            BrokerConnection.provider == TRADINGVIEW_PAPER_PROVIDER,
        )
    )
    if connection is None:
        connection = BrokerConnection(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            provider=TRADINGVIEW_PAPER_PROVIDER,
            environment="file-import",
            status="configured",
            config_reference=None,
        )
        db.add(connection)
        db.flush()

    imported_executions = 0
    imported_fills = 0
    duplicate_executions = 0
    imported_trade_ids: set[object] = set()
    try:
        for item in export.events:
            event = item.event
            event_hash = _event_hash(event)
            existing = db.scalar(
                select(ExecutionEvent).where(
                    ExecutionEvent.workspace_id == scope.workspace_id,
                    ExecutionEvent.account_id == scope.account_id,
                    ExecutionEvent.connection_id == connection.id,
                    ExecutionEvent.external_event_id == event.external_id,
                )
            )
            if existing is not None:
                if existing.source_payload_hash != event_hash:
                    raise TradingViewImportError(
                        "This export changes a TradingView execution that was already "
                        "imported. Nothing was changed; keep the prior record and review "
                        "the CSV before retrying."
                    )
                duplicate_executions += 1
                continue

            instrument = get_or_create_instrument(db, event.instrument or "")
            get_or_create_mapping(
                db,
                instrument,
                provider=TRADINGVIEW_PAPER_PROVIDER,
                external_symbol=item.source_row.source_symbol,
                venue=item.source_row.source_venue,
            )
            trade = None
            if event.trade_effects:
                lifecycle_trades = _apply_trade_effects(
                    db,
                    connection=connection,
                    event=event,
                    instrument_id=instrument.id,
                )
                trade = _primary_trade(event, lifecycle_trades)
                if trade is None:
                    raise TradingViewImportError(
                        "TradingView history could not be linked to a deterministic trade "
                        "lifecycle. Nothing was changed."
                    )
                imported_trade_ids.add(trade.id)
            execution = ExecutionEvent(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                connection_id=connection.id,
                trade_id=trade.id if trade is not None else None,
                external_event_id=event.external_id,
                external_order_id=event.external_order_id,
                external_trade_id=event.external_trade_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                source_payload_hash=event_hash,
                provider_metadata={
                    "normalized_source": event.source,
                    "export_kind": export.export_kind,
                    "source_file_sha256": export.source_sha256,
                    "source_row": item.source_row.row_number,
                    "source_symbol": item.source_row.source_symbol,
                    "source_venue": item.source_row.source_venue,
                    "order_status": item.source_row.status,
                    "order_side": item.source_row.side,
                    "order_quantity": str(item.source_row.quantity),
                    "intended_price": (
                        str(item.source_row.intended_price)
                        if item.source_row.intended_price is not None
                        else None
                    ),
                    "order_type": item.source_row.order_type,
                    "placing_time": (
                        item.source_row.placing_time.isoformat()
                        if item.source_row.placing_time is not None
                        else None
                    ),
                    "trade_effects": [
                        _trade_effect_metadata(effect) for effect in event.trade_effects
                    ],
                    "pnl_supplied_by_export": event.realized_pnl is not None,
                    "history_coverage": export.history_coverage,
                    "lifecycle_evidence": export.lifecycle_evidence,
                    "lifecycle_authoritative": export.lifecycle_evidence
                    != "execution_only",
                },
            )
            db.add(execution)
            db.flush()
            if event.event_type == "order_fill":
                if event.quantity is None or event.price is None:
                    raise TradingViewImportError(
                        "A filled TradingView order is missing normalized fill data. "
                        "Nothing was changed."
                    )
                db.add(
                    Fill(
                        workspace_id=scope.workspace_id,
                        account_id=scope.account_id,
                        connection_id=connection.id,
                        trade_id=trade.id if trade is not None else None,
                        execution_event_id=execution.id,
                        instrument_id=instrument.id,
                        external_fill_id=event.external_id,
                        side="buy" if event.quantity > 0 else "sell",
                        quantity=abs(event.quantity),
                        price=event.price,
                        commission=event.commission,
                        financing=None,
                        guaranteed_execution_fee=None,
                        half_spread_cost=None,
                        realized_pnl=event.realized_pnl,
                        occurred_at=event.occurred_at,
                    )
                )
                imported_fills += 1
            imported_executions += 1
        db.commit()
    except Exception:
        db.rollback()
        raise

    return TradingViewImportResult(
        export_kind=export.export_kind,
        imported_executions=imported_executions,
        imported_fills=imported_fills,
        imported_trades=len(imported_trade_ids),
        duplicate_executions=duplicate_executions,
        rows_ignored=export.rows_ignored,
        instruments=export.instruments,
        realized_pnl_available=export.realized_pnl_available,
        history_coverage=export.history_coverage,
        lifecycle_evidence=export.lifecycle_evidence,
    )
