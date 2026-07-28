# MetaTrader read-only bridge

## Purpose and boundary

Trading Agent can run on macOS, Linux, or Windows while MetaTrader runs elsewhere. The agent
connects to six fixed `GET` endpoints and has no generic HTTP, order, modification,
cancellation, or close-position method.

The included companion service supports MetaTrader 5 through the official Windows terminal
and Python integration. MetaTrader 4 can use the same wire contract from a separately reviewed
EA or local bridge. MT4's terminal must allowlist only the bridge destination.

Live quotes and requested candle windows remain in bounded memory. PostgreSQL stores broker
execution events, fills and costs, account snapshots, net position snapshots, connector
cursors, source hashes, and reconciliation results. It does not store every tick.

## Run the MT5 companion on Windows

Use a dedicated Windows machine or VPS with MT5 installed and logged into the intended
practice account:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[metatrader]"
```

Set these in the bridge machine's private environment or `.env`:

```text
METATRADER_BRIDGE_TOKEN=at-least-32-random-characters
METATRADER_ACCOUNT_ID=12345678
METATRADER_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
```

Start:

```powershell
trading-agent-mt5-bridge
```

It binds to `127.0.0.1:8765` by default. For another computer, prefer an authenticated private
HTTPS tunnel or reverse proxy while leaving the bridge on loopback. Direct network binding
requires `METATRADER_BRIDGE_BIND_HOST`, `METATRADER_BRIDGE_ALLOW_NETWORK=true`,
`METATRADER_BRIDGE_TLS_CERTFILE`, and `METATRADER_BRIDGE_TLS_KEYFILE`; the TLS files must be
regular non-symlink files and mode `0600`. The included service will not expose plain remote
HTTP. The client's `METATRADER_ALLOW_INSECURE_REMOTE` override exists only for a separately
reviewed private bridge implementation and does not weaken the included server.

Do not put the bearer token in a URL. The client sends it only in the `Authorization` header.
Do not reuse a model key, broker password, or terminal password.

## Connect Trading Agent

On the machine running Trading Agent:

```text
BROKER_PROVIDER=metatrader
METATRADER_PLATFORM=mt5
METATRADER_BRIDGE_URL=https://private-bridge.example
METATRADER_BRIDGE_TOKEN=the-same-dedicated-token
METATRADER_ACCOUNT_ID=12345678
METATRADER_MODE=practice
```

Run:

```bash
trade broker configure-metatrader --label mt5-practice
trade broker quote XAUUSD
trade broker sync
trade data status
```

Registration reads `/v1/health` and `/v1/account`, requires a read-only attestation, and
refuses an account or platform mismatch before writing the connection record.

## History behavior

With no cursor, the first sync takes a present-time baseline. This prevents a surprise
multi-year import. Before that first sync, import deliberate history with:

```bash
trade broker sync --from-cursor 2020-01-01T00:00:00Z
```

The stored cursor is `<time_milliseconds>:<deal_ticket>`. Each bridge response is bounded to
5,000 deals and the client response is byte-bounded. Run sync again to continue when more
history remains. Broker event IDs make repeated imports idempotent.

MT5 deal records preserve their position ID, direction, volume, price, profit, commission,
fee, and swap. A historical deal alone does not always prove whether an exit was a partial
reduction, full close, or reversal. The included service therefore marks these histories as
non-inferable: fills and current snapshots are stored, but the ledger does not manufacture a
trade lifecycle link. A future lifecycle reconstruction must prove the complete position
history before adding authoritative `trade_effects`.

## Bridge contract for MT4 or another terminal host

Every route requires `Authorization: Bearer <token>` and returns one JSON object:

- `GET /v1/health`: `platform`, `account_id`, `read_only=true`,
  `terminal_connected`, `server_time`.
- `GET /v1/quote?instrument=XAUUSD`: `symbol`, `bid`, `ask`, and ISO `time` or
  Unix `time_msc`.
- `GET /v1/candles?instrument=XAUUSD&timeframe=M5&count=300`: `candles`, each with
  time, OHLC, optional volume, and completion state.
- `GET /v1/account`: account ID, currency, balance, equity, margin used/available, time.
- `GET /v1/positions`: account ID and net-per-symbol positions with position ID, symbol,
  signed net quantity, optional average price and unrealized P/L, time.
- `GET /v1/events?cursor=...`: account ID, normalized execution events, opaque
  `next_cursor`, and optional `has_more`.

Events may include authoritative `trade_effects` (`opened`, `reduced`, or `closed`). When a
bridge cannot prove lifecycle state, it must send no effects and set
`infer_trade_open=false`. Source timestamps must come from the terminal; retrieval timestamps
are added by the client.

The bridge must never add an order route to this service. A future execution product belongs
behind a separately reviewed preview, deterministic risk, explicit approval, and audit
boundary.
