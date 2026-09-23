# MT5 on Mac: read-only MQL companion (experimental)

This is an experimental second MetaTrader route. The EA runs
inside MT5 (including its Wine-based Mac installation); the receiver runs in native
Python on the same computer. It does **not** use the Windows-only `MetaTrader5`
Python package. The existing Windows bridge is unchanged.

**Not yet production-qualified:** MetaEditor compilation and a Mac/Wine demo
account-to-receiver transfer, gold bid/ask retrieval, saved-pairing reconnection,
and a successful automatic code refresh have been verified. Full-history import
and recovery from failed live updates remain separate qualification gates. MT4 is
not supported by this source.

## What this version does

- Every ten seconds: send account balance/equity/margin, open position legs, the
  requested symbol's latest available bid/ask, up to 50 candles each on H4/M15/M5/M1,
  and up to 500 deals from a seven-day server-time window. Candle volume is tick
  count, not contracts. The latest bar stays provisional.
- On demand: read any of MT5's 21 standard timeframes, with up to 5,000 candles per
  HTTP request and older pages selected by broker bar-open time. The chat tool uses
  up to 500 bars per page to keep model context bounded. The four small automatic
  snapshots are a startup preview, not the limit of candle access.
- Retain deal/order/position IDs, deal type and entry classification, volume in
  **lots**, price, profit, commission, swap and fee. Cash movements are preserved
  as deal records, not mislabeled as fills.
- Pin both the account login and broker server; changing either stops transmission.
- Authenticate both sending and reading with a dedicated token. A private saved
  preset can now be reused across receiver restarts without changing EA inputs.
- Keep only one bounded snapshot in memory. Stop the receiver to discard it.
- Reject malformed, oversized, replayed and out-of-order snapshots. Reject stale
  capture times; stop serving account data after 45 seconds without an update.

**No journal import yet.** The receiver now supports the existing read-only broker
connector and can be registered through its policy-confirmed setup flow. The receiver
itself does not update PostgreSQL, reconcile positions, or expose new model tools.
No order submission, modification or cancellation code exists in the EA/receiver.

## Using the existing agent tools

The adapter exposes authenticated GET account, positions, symbols, quote and candle
routes. `get_broker_state` now also returns bounded companion evidence when the
selected account uses this receiver: the quote, individual position legs, the last
10 bars per available timeframe, and the last 20 deals. This remains source-labelled
data inside the existing untrusted-evidence envelope and policy execution hook.
Account identifiers are removed before returning that context to the model.
OANDA and the Windows bridge retain their existing behavior.

One-time registration can reuse the saved pairing instead of asking for a token:

```bash
trade broker configure-metatrader --label mt5-demo --companion-preset /absolute/path/to/private-preset.set
```

This verifies account/server/transport, asks permission to register, stores the token
in the OS credential vault, and selects the registered MT5 account in managed settings.
It does not delete an existing OANDA account or import trades. Declining changes nothing.
The receiver and MT5 must remain open; receiver auto-start is still a follow-up.

Normalized quote/candle endpoints require `--broker-timezone` on the receiver with a
**broker-confirmed IANA timezone**, not the user's display timezone. Until configured,
account/position reads and clearly labelled raw quote/candle/deal evidence remain
available through `get_broker_state`. The adapter refuses to fabricate UTC timestamps.
Ambiguous and nonexistent daylight-saving times are rejected, not guessed. A recent
snapshot never proves that its last tick is fresh.

`get_recent_candles` now uses the companion's raw candle-read route when connected
to MT5. It returns human-readable **broker wall times**, source, symbol, timeframe,
tick counts, provisional-bar flags, requested/returned counts and a pagination cursor.
It therefore works before UTC qualification without pretending the timestamps are UTC.
Pass the returned `next_before_broker_time` as `before_broker_time` to request the
previous page; use null for the latest bars. No candles or research datasets are saved.

Supported timeframes: M1, M2, M3, M4, M5, M6, M10, M12, M15, M20, M30, H1, H2, H3,
H4, H6, H8, H12, D1, W1 and MN1. Access remains scoped to the paired broker symbol.
It does not guarantee unlimited history: broker availability and terminal Max Bars
still apply. Partial pages and loading/unavailable responses are explicit; they do
not establish full-history coverage. See [MT5 CopyRates](https://www.mql5.com/en/docs/series/copyrates)
and [standard timeframes](https://www.mql5.com/en/docs/constants/chartconstants/enum_timeframes).

The EA polls once per second for a strict candle-only request: random request ID,
allowlisted timeframe, bounded count and optional broker-time upper bound. No symbol,
URL, path, script, order parameters or arbitrary commands are accepted from that lane.
At most four reads can wait for up to eight seconds each. Results must match the
request, pinned account/server/symbol and fresh capture time. Late/replayed/malformed
results are rejected. The automatic snapshots remain on a ten-second cadence.

`sync_broker_history` still requires the existing mutation confirmation, and the
companion rejects ingestion before any cursor advance or trade import. Reliable
resumable history and lifecycle reconstruction remain unfinished. Use read-only
recent activity for inspection; do not interpret an empty imported ledger as no
activity in MT5. Cancelled/rejected orders are not captured in this deal-only stream.

## One-time test setup

1. Open MT5, log into the intended account (prefer a demo for this test), and note
   the exact login number, broker server name and gold symbol (including suffix).
   The companion selects that exact symbol in Market Watch to subscribe to its
   market data. It never substitutes an instrument automatically. Do not share
   your broker password with the receiver.
2. From the Trading Agent checkout, using its installed virtual environment:

   ```bash
   ./.venv/bin/python -m app.metatrader_companion
   ```

   Answer the three questions. No `.env` changes are needed. Keep the terminal open.
   Installed distributions also provide `trading-agent-mt5-companion`.
3. The receiver prints the path to `TradingAgentReadOnly.mq5`. In MT5 choose
   **File → Open Data Folder**, copy that file into **MQL5 → Experts**, open it
   in MetaEditor, and compile. Stop here if compilation fails; capture compiler
   errors, not private tokens/account screenshots, for debugging.
4. In MT5 **Tools → Options → Expert Advisors**, allow WebRequest only to
   `http://127.0.0.1:8766` (or the printed port). No DLL imports are needed. This EA
   has no trading functions; do not enable trading permissions just for this test.
5. Attach the EA to one chart. Copy the printed account, server, symbol, port and
   temporary token into its inputs. The **Experts** log should report:
   `Receiving confirmed. Read-only snapshot accepted; journal unchanged.`

The token grants access to this account snapshot. Do not share screenshots of it,
commit exported EA presets, or put it in URLs. MT5 may retain EA inputs locally.
Restarting without `--preset` creates a new token. To preserve pairing, restart using
`python -m app.metatrader_companion --preset /absolute/path/to/private-preset.set`.
The preset must be owned by you, have mode 0600, and not be symlinked. It contains
the local receiver token, not a broker password. Tokens are not printed when reused.
EA reattachment/recompilation preserves sequence ordering through terminal
uptime; restart the receiver and pairing after a Wine/OS restart. Only one EA
instance should send to a receiver.

## Controlled local code refresh

`python -m app.metatrader_refresh` is an operator utility, not an LLM tool or
remote-command endpoint. It accepts the private `--preset`, MT5 `--terminal` data
directory, `--wine-prefix`, and optionally the vendor `--wine` executable path.
It always compiles this package's fixed `TradingAgentReadOnly.mq5` source; it does
not fetch or accept arbitrary source URLs or tell the terminal to run commands.

The update sequence is:

1. Read the receiver's authenticated status; require an active refresh-capable EA.
2. Compile in an isolated temporary directory, with a 30-second limit. Require
   zero errors, zero warnings and a fresh nonempty binary, not just an exit code.
3. Back up our existing source/binary. Atomically install the candidate, then write
   a fixed local 64-character build-hash marker in `MQL5/Files`.
4. On its timer the companion saves/reapplies **its own chart's** template to reload
   itself, preserving inputs. This also reinitializes indicators on that chart;
   other charts and the terminal login are untouched. Trading and DLL permissions
   are never enabled. Reload is refused if trading is permitted both globally and
   for the EA, or if DLL permission is enabled.
5. Require a fresh authenticated snapshot naming the exact expected build hash.
   A copied file or healthy old build is not success. If no acknowledgement arrives,
   restore previous files and report that the active runtime needs inspection;
   restoring files alone does not prove the old EA is running.

An attempt marker prevents a failed/cached reload from looping through repeated
initializations. The updater serializes deployments with a local lock. Marker and
backup paths are fixed; symlinked targets are refused. Private template files are
reserved in both supported profile layouts because templates include the paired
inputs. Do not share these files or commit them to Git.

**One-time bootstrap:** old EAs cannot act on a reload marker they were never
programmed to read. `--bootstrap` installs the first refresh-capable build and
reports `bootstrap_installed_not_active`, not success. Close/reopen MT5 normally
(with the companion saved on its chart), or reattach once using the existing
preset, then verify the new build. The updater does not kill/restart your terminal.
After bootstrap, ordinary updates use the local marker/acknowledgement path.
Receiver auto-start at agent launch is not implemented by this utility.

### Observed Mac/Wine refresh test — 2026-09-22

After a normal MT5 restart loaded the bootstrap build, the saved pairing reconnected
without new input. The local updater then compiled/deployed version 1.02. MT5's
companion logged a queued template refresh and reinitialized itself; the receiver
acknowledged the exact new source-build hash and continued receiving XAUUSD bid/ask.
No chart drag, preset reload, terminal restart, or trading-permission change was
needed for that second update. The generated template remained mode 0600. This
confirms the successful-update path on this installation, not every Wine/broker
combination or failed-update recovery. Private account records are not included here.

References: [template reapplication and permission restrictions](https://www.mql5.com/en/docs/chart_operations/chartapplytemplate),
[MetaEditor compiler interface](https://www.metatrader5.com/en/metaeditor/help/beginning/integration_ide).

Version 1.04 was subsequently compiled and refreshed through the same mechanism.
Live read-only checks returned 60 gold candles on every one of the 21 timeframes.
Two H1 requests returned 500 bars each, with the second page strictly earlier than
the first and no overlap. Broker times remained explicitly unconverted; no candle
dataset, journal records or orders were written. This qualifies those reads on this
installation, not unlimited history or every broker.

## Inspect safely

Use an authenticated local HTTP client with `Authorization: Bearer <temporary token>`:

- `GET /v1/companion/health`: receiving/awaiting/stale, receive time and age. A recent
  transmission is **not** a guarantee that the market is open or the quote is fresh.
- `GET /v1/companion/snapshot`: the bounded snapshot, when recently received.
- `POST /v1/companion/snapshot`: EA upload, not a remote-command endpoint.
- `GET /v1/companion/candles`: on-demand raw candle page for the paired symbol.
- `GET /v1/companion/candle-request`: fixed candle-only request for the EA, or 204.
- `POST /v1/companion/candle-result`: matching validated response; never persisted.

All endpoints require authentication. No docs UI, CORS, public listener, forwarded
headers, database writes or access logging. The launcher binds to IPv4 loopback
only. Do not expose it through a tunnel or reverse proxy. Any process/user that
can read the token can read snapshots; this is not isolation from malware on your
own machine. It does not replace broker access controls.

## Time and history limitations

`captured_at` is the terminal computer's UTC clock; `received_at` is the receiver's
UTC clock. They are distinct from **broker market timestamps**. Quote/deal times
and the selected history interval are retained as raw broker-server values with
`market_time_basis=broker_server_unconverted`. No guessed timezone/DST conversion
is applied by default. An explicit broker timezone enables normalized market reads;
UTC normalization and history coverage must be validated before enabling imports.

History is always labeled `recent_window`, never complete account history. More
than 500 deals sets `truncated=true`; an empty list means no deals returned for
that window, not “this account has never traded.” Failed history reads do not send
a fabricated empty list. Cancelled/rejected orders are not deal records and are
**not yet captured**; the next order-history implementation must retain them.
No lifecycle inference or lot-to-unit conversion is performed.

Keep the Mac awake and MT5 connected. A closed terminal, sleep, or network failure
stops updates; old data becomes unavailable. If there is no quote, `quote=null`
is explicit. A previously recorded tick on a closed market is not called live.
Missing quotes now produce a specific Experts-log diagnostic: unavailable broker
symbol (with bounded gold-name candidates), subscription failure, waiting for the
first tick, or unusable bid/ask. Custom/local symbols are not accepted as broker quotes.

## Qualification and next slices

Before integrating with the journal:

1. Compile in MetaEditor; test Wine → loopback WebRequest and compare account,
   quote and deal IDs against MT5. Record MT5/Wine/macOS versions, no private data.
2. Test account/server switch, disconnected terminal, missing quote, receiver/EA
   restart and machine sleep; verify stale/rejection behavior.
3. Add timestamp qualification and resumable, idempotent history batches, including
   cancelled/rejected orders, position identifiers and partial-close scenarios.
4. Then add account-scoped PostgreSQL ingestion through existing policy/confirmation
   hooks, execution-to-journal pairing, and guided `/connect` setup. No new model
   tool may bypass the runtime policy.

References: [official Mac installation](https://www.metatrader5.com/en/terminal/help/start_advanced/install_mac),
[WebRequest and its allowlist/tester restrictions](https://www.mql5.com/en/docs/network/webrequest),
[HistorySelect server-time semantics](https://www.mql5.com/en/docs/trading/historyselect),
[TimeGMT computer-clock semantics](https://www.mql5.com/en/docs/dateandtime/timegmt).
