"""Companion read-only bridge for a Windows-hosted MetaTrader 5 terminal.

The bridge intentionally exposes no order, modification, or cancellation route. Run it
beside a logged-in MT5 terminal, then connect Trading Agent from the same machine or through
a separately secured private HTTPS tunnel.
"""

import os
import secrets
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query

from app.config import get_settings, secret_value

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_MAX_HISTORY_EVENTS = 5_000
_MAX_HISTORY_WINDOW = timedelta(days=90)
_MIN_HISTORY_WINDOW = timedelta(seconds=1)


class MetaTraderTerminalError(RuntimeError):
    pass


def _values(item: Any) -> Mapping[str, Any]:
    if hasattr(item, "_asdict"):
        return item._asdict()
    if isinstance(item, Mapping):
        return item
    if getattr(item, "dtype", None) is not None and item.dtype.names:
        return {name: item[name] for name in item.dtype.names}
    raise MetaTraderTerminalError("MetaTrader returned an unsupported record")


def _iso_from_seconds(value: Any) -> str:
    return datetime.fromtimestamp(int(value), tz=UTC).isoformat()


def _cursor_start(cursor: str) -> tuple[datetime, tuple[int, int]]:
    if ":" in cursor and cursor.partition(":")[0].isdigit():
        milliseconds, _, ticket = cursor.partition(":")
        key = (int(milliseconds), int(ticket or "0"))
        return (
            datetime.fromtimestamp(key[0] / 1_000, tz=UTC) - timedelta(milliseconds=1),
            key,
        )
    parsed = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("history cursor timestamp must include a timezone")
    parsed = parsed.astimezone(UTC)
    return parsed, (int(parsed.timestamp() * 1_000), 0)


class MetaTrader5Terminal:
    """Serialize official terminal IPC calls and normalize their return values."""

    platform = "mt5"

    def __init__(
        self,
        module: Any,
        *,
        account_id: str,
        terminal_path: Path | None = None,
    ) -> None:
        self.module = module
        self.account_id = account_id
        self.terminal_path = terminal_path
        self._lock = threading.Lock()

    def connect(self) -> None:
        with self._lock:
            initialized = (
                self.module.initialize(str(self.terminal_path))
                if self.terminal_path is not None
                else self.module.initialize()
            )
            if not initialized:
                raise MetaTraderTerminalError(
                    f"MT5 initialize failed with code {self.module.last_error()[0]}"
                )
            account = self.module.account_info()
            if account is None:
                raise MetaTraderTerminalError(
                    f"MT5 account read failed with code {self.module.last_error()[0]}"
                )
            if str(account.login) != self.account_id:
                self.module.shutdown()
                raise MetaTraderTerminalError(
                    "connected MT5 account does not match METATRADER_ACCOUNT_ID"
                )

    def close(self) -> None:
        with self._lock:
            self.module.shutdown()

    def _required(self, value: Any, operation: str) -> Any:
        if value is None:
            raise MetaTraderTerminalError(
                f"MT5 {operation} failed with code {self.module.last_error()[0]}"
            )
        return value

    def health(self) -> dict[str, Any]:
        with self._lock:
            terminal = self._required(self.module.terminal_info(), "terminal_info")
            account = self._required(self.module.account_info(), "account_info")
            return {
                "platform": self.platform,
                "account_id": str(account.login),
                "read_only": True,
                "terminal_connected": bool(terminal.connected),
                "trade_allowed_by_bridge": False,
                "server_time": datetime.now(UTC).isoformat(),
            }

    def quote(self, instrument: str) -> dict[str, Any]:
        with self._lock:
            tick = self._required(
                self.module.symbol_info_tick(instrument),
                "symbol_info_tick",
            )
            return {
                "symbol": instrument,
                "bid": str(tick.bid),
                "ask": str(tick.ask),
                "time_msc": int(tick.time_msc),
            }

    def candles(
        self,
        instrument: str,
        timeframe: str,
        count: int,
    ) -> dict[str, Any]:
        constant_name = f"TIMEFRAME_{timeframe.upper()}"
        mt5_timeframe = getattr(self.module, constant_name, None)
        if mt5_timeframe is None:
            raise ValueError(f"unsupported MT5 timeframe: {timeframe}")
        with self._lock:
            rates = self._required(
                self.module.copy_rates_from_pos(instrument, mt5_timeframe, 0, count),
                "copy_rates_from_pos",
            )
            items = []
            for index, raw in enumerate(rates):
                rate = _values(raw)
                items.append(
                    {
                        "time": _iso_from_seconds(rate["time"]),
                        "open": str(rate["open"]),
                        "high": str(rate["high"]),
                        "low": str(rate["low"]),
                        "close": str(rate["close"]),
                        "volume": str(
                            rate.get("real_volume") or rate.get("tick_volume") or 0
                        ),
                        "complete": index < len(rates) - 1,
                    }
                )
            return {"candles": items}

    def account(self) -> dict[str, Any]:
        with self._lock:
            account = self._required(self.module.account_info(), "account_info")
            return {
                "account_id": str(account.login),
                "currency": str(account.currency),
                "balance": str(account.balance),
                "equity": str(account.equity),
                "margin_used": str(account.margin),
                "margin_available": str(account.margin_free),
                "time": datetime.now(UTC).isoformat(),
            }

    def positions(self) -> dict[str, Any]:
        with self._lock:
            raw_positions = self._required(self.module.positions_get(), "positions_get")
            grouped: dict[str, dict[str, Any]] = {}
            for raw in raw_positions:
                position = _values(raw)
                symbol = str(position["symbol"])
                signed_volume = Decimal(str(position["volume"]))
                if int(position["type"]) == int(self.module.POSITION_TYPE_SELL):
                    signed_volume = -signed_volume
                current = grouped.setdefault(
                    symbol,
                    {
                        "position_id": symbol,
                        "symbol": symbol,
                        "net_quantity": Decimal("0"),
                        "unrealized_pnl": Decimal("0"),
                        "legs": [],
                        "latest_time_msc": 0,
                    },
                )
                current["net_quantity"] += signed_volume
                current["unrealized_pnl"] += Decimal(str(position["profit"]))
                current["legs"].append(
                    (signed_volume, Decimal(str(position["price_open"])))
                )
                current["latest_time_msc"] = max(
                    current["latest_time_msc"],
                    int(position.get("time_msc") or int(position["time"]) * 1_000),
                )

            items = []
            for current in grouped.values():
                legs = current.pop("legs")
                signs = {volume > 0 for volume, _price in legs if volume != 0}
                total_absolute = sum((abs(volume) for volume, _price in legs), Decimal("0"))
                average_price = None
                if len(signs) == 1 and total_absolute:
                    average_price = sum(
                        (abs(volume) * price for volume, price in legs),
                        Decimal("0"),
                    ) / total_absolute
                latest_time_msc = current.pop("latest_time_msc")
                items.append(
                    {
                        **current,
                        "net_quantity": str(current["net_quantity"]),
                        "average_price": (
                            str(average_price) if average_price is not None else None
                        ),
                        "unrealized_pnl": str(current["unrealized_pnl"]),
                        "time_msc": latest_time_msc,
                    }
                )
            return {"account_id": self.account_id, "positions": items}

    def events(self, cursor: str | None) -> dict[str, Any]:
        if cursor is None:
            baseline = f"{int(datetime.now(UTC).timestamp() * 1_000)}:0"
            return {
                "account_id": self.account_id,
                "events": [],
                "next_cursor": baseline,
                "baseline_only": True,
            }
        start, key = _cursor_start(cursor)
        with self._lock:
            now = datetime.now(UTC)
            window_end = min(start + _MAX_HISTORY_WINDOW, now)
            raw_deals = self._required(
                self.module.history_deals_get(start, window_end),
                "history_deals_get",
            )
            while (
                len(raw_deals) > _MAX_HISTORY_EVENTS
                and window_end - start > _MIN_HISTORY_WINDOW
            ):
                window_end = start + max(
                    _MIN_HISTORY_WINDOW,
                    (window_end - start) / 2,
                )
                raw_deals = self._required(
                    self.module.history_deals_get(start, window_end),
                    "history_deals_get",
                )
            raw_positions = self._required(
                self.module.positions_get(),
                "positions_get",
            )
            buy_type = int(self.module.DEAL_TYPE_BUY)
            sell_type = int(self.module.DEAL_TYPE_SELL)
            entry_in = getattr(self.module, "DEAL_ENTRY_IN", None)
            entry_out = getattr(self.module, "DEAL_ENTRY_OUT", None)
            entry_out_by = getattr(self.module, "DEAL_ENTRY_OUT_BY", None)
            exit_entries = frozenset(
                value for value in (entry_out, entry_out_by) if value is not None
            )
            active_position_ids = {
                str(
                    _values(position).get("identifier")
                    or _values(position).get("ticket")
                )
                for position in raw_positions
            }
            deals = []
            for raw in raw_deals:
                deal = _values(raw)
                deal_type = int(deal["type"])
                if deal_type not in {buy_type, sell_type}:
                    continue
                deal_key = (
                    int(deal.get("time_msc") or int(deal["time"]) * 1_000),
                    int(deal["ticket"]),
                )
                if deal_key <= key:
                    continue
                deals.append((deal_key, deal))
            deals.sort(key=lambda item: item[0])
            truncated = len(deals) > _MAX_HISTORY_EVENTS
            deals = deals[:_MAX_HISTORY_EVENTS]
            has_more = truncated or window_end < now
            final_exit_keys: dict[str, tuple[int, int] | None] = {}

            def final_exit_key(position_id: str) -> tuple[int, int] | None:
                cached = final_exit_keys.get(position_id)
                if position_id in final_exit_keys:
                    return cached
                try:
                    provider_position_id: int | str = int(position_id)
                except ValueError:
                    provider_position_id = position_id
                position_deals = self._required(
                    self.module.history_deals_get(position=provider_position_id),
                    "history_deals_get(position)",
                )
                exit_keys = []
                for raw_position_deal in position_deals:
                    position_deal = _values(raw_position_deal)
                    position_entry = position_deal.get("entry")
                    if (
                        int(position_deal["type"]) in {buy_type, sell_type}
                        and position_entry in exit_entries
                    ):
                        exit_keys.append(
                            (
                                int(
                                    position_deal.get("time_msc")
                                    or int(position_deal["time"]) * 1_000
                                ),
                                int(position_deal["ticket"]),
                            )
                        )
                result = max(exit_keys, default=None)
                final_exit_keys[position_id] = result
                return result

            events = []
            for deal_key, deal in deals:
                quantity = Decimal(str(deal["volume"]))
                if int(deal["type"]) == sell_type:
                    quantity = -quantity
                commission = Decimal(str(deal.get("commission") or 0))
                commission += Decimal(str(deal.get("fee") or 0))
                position_id = str(deal["position_id"])
                trade_effects: list[dict[str, str | None]] = []
                entry = deal.get("entry")
                if entry_in is not None and entry == entry_in:
                    trade_effects.append(
                        {
                            "external_trade_id": position_id,
                            "effect": "opened",
                            "quantity": str(quantity),
                            "realized_pnl": None,
                        }
                    )
                elif entry in exit_entries:
                    is_final_known_exit = (
                        position_id not in active_position_ids
                        and final_exit_key(position_id) == deal_key
                    )
                    trade_effects.append(
                        {
                            "external_trade_id": position_id,
                            "effect": "closed" if is_final_known_exit else "reduced",
                            "quantity": str(quantity),
                            "realized_pnl": str(deal.get("profit") or 0),
                        }
                    )
                events.append(
                    {
                        "event_id": str(deal["ticket"]),
                        "event_type": "deal_fill",
                        "time_msc": deal_key[0],
                        "symbol": str(deal["symbol"]),
                        "order_id": str(deal["order"]),
                        "trade_id": position_id,
                        "quantity": str(quantity),
                        "price": str(deal["price"]),
                        "realized_pnl": str(deal.get("profit") or 0),
                        "commission": str(commission),
                        "financing": str(deal.get("swap") or 0),
                        "trade_effects": trade_effects,
                        "infer_trade_open": False,
                    }
                )
            if deals:
                next_key = deals[-1][0]
            elif window_end < now:
                next_key = (int(window_end.timestamp() * 1_000), 0)
            else:
                next_key = key
            return {
                "account_id": self.account_id,
                "events": events,
                "next_cursor": f"{next_key[0]}:{next_key[1]}",
                "has_more": has_more,
            }


def create_bridge_app(terminal: MetaTrader5Terminal, *, token: str) -> FastAPI:
    if len(token) < 32:
        raise ValueError("MetaTrader bridge token must contain at least 32 characters")
    app = FastAPI(
        title="Trading Agent MetaTrader Read-Only Bridge",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorize(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid bridge authorization")

    read_only = [Depends(authorize)]

    @app.get("/v1/health", dependencies=read_only)
    def health() -> dict[str, Any]:
        return terminal.health()

    @app.get("/v1/quote", dependencies=read_only)
    def quote(
        instrument: Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")],
    ) -> dict[str, Any]:
        return terminal.quote(instrument)

    @app.get("/v1/candles", dependencies=read_only)
    def candles(
        instrument: Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")],
        timeframe: Annotated[str, Query(pattern=r"^[A-Za-z0-9]{1,12}$")],
        count: Annotated[int, Query(ge=1, le=5_000)] = 300,
    ) -> dict[str, Any]:
        return terminal.candles(instrument, timeframe, count)

    @app.get("/v1/account", dependencies=read_only)
    def account() -> dict[str, Any]:
        return terminal.account()

    @app.get("/v1/positions", dependencies=read_only)
    def positions() -> dict[str, Any]:
        return terminal.positions()

    @app.get("/v1/events", dependencies=read_only)
    def events(
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ) -> dict[str, Any]:
        try:
            return terminal.events(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


def _load_mt5_module() -> Any:
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError(
            "Install the Windows bridge dependency with "
            "`pip install 'trading-agent[metatrader]'`"
        ) from exc
    return mt5


def run() -> None:
    settings = get_settings()
    token = secret_value(settings.metatrader_bridge_token)
    account_id = secret_value(settings.metatrader_account_id)
    if not token or not account_id:
        raise RuntimeError(
            "METATRADER_BRIDGE_TOKEN and METATRADER_ACCOUNT_ID are required"
        )
    host = os.environ.get("METATRADER_BRIDGE_BIND_HOST", "127.0.0.1")
    allow_network = os.environ.get("METATRADER_BRIDGE_ALLOW_NETWORK", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    if host.casefold() not in _LOOPBACK_HOSTS and not allow_network:
        raise RuntimeError(
            "non-loopback bridge binding requires METATRADER_BRIDGE_ALLOW_NETWORK=true"
        )
    certificate_value = os.environ.get("METATRADER_BRIDGE_TLS_CERTFILE")
    private_key_value = os.environ.get("METATRADER_BRIDGE_TLS_KEYFILE")
    if bool(certificate_value) != bool(private_key_value):
        raise RuntimeError(
            "both METATRADER_BRIDGE_TLS_CERTFILE and "
            "METATRADER_BRIDGE_TLS_KEYFILE are required for TLS"
        )
    if host.casefold() not in _LOOPBACK_HOSTS and not certificate_value:
        raise RuntimeError(
            "non-loopback bridge binding requires direct TLS; configure both "
            "METATRADER_BRIDGE_TLS_CERTFILE and METATRADER_BRIDGE_TLS_KEYFILE, "
            "or bind to loopback behind an authenticated HTTPS tunnel"
        )
    certificate = Path(certificate_value).expanduser() if certificate_value else None
    private_key = Path(private_key_value).expanduser() if private_key_value else None
    for tls_file in (certificate, private_key):
        if tls_file is None:
            continue
        if tls_file.is_symlink() or not tls_file.is_file():
            raise RuntimeError("MetaTrader bridge TLS files must be regular non-symlink files")
        if hasattr(os, "getuid") and tls_file.stat().st_uid != os.getuid():
            raise RuntimeError(
                "MetaTrader bridge TLS files must be owned by the current user"
            )
        if os.name != "nt" and tls_file.stat().st_mode & 0o077:
            raise RuntimeError("MetaTrader bridge TLS files must have mode 600")
    terminal_path_value = os.environ.get("METATRADER_TERMINAL_PATH")
    terminal = MetaTrader5Terminal(
        _load_mt5_module(),
        account_id=account_id,
        terminal_path=Path(terminal_path_value) if terminal_path_value else None,
    )
    terminal.connect()
    try:
        uvicorn.run(
            create_bridge_app(terminal, token=token),
            host=host,
            port=int(os.environ.get("METATRADER_BRIDGE_PORT", "8765")),
            access_log=False,
            ssl_certfile=str(certificate.resolve()) if certificate is not None else None,
            ssl_keyfile=str(private_key.resolve()) if private_key is not None else None,
        )
    finally:
        terminal.close()


if __name__ == "__main__":
    run()
