# Data ingestion

For deterministic provider simulations, fault injection, bounded soak testing, and the
difference between simulated and real verification evidence, see
[`integration-reliability.md`](integration-reliability.md).

Trading Agent uses four ingestion lanes because the data has different trust, retention, and
strategy-isolation requirements.

## Select the destination account first

Broker history, TradingView alerts, conversations, decisions, mindset, evidence, tests, and
review memory are account-owned. Before importing or syncing, verify the active destination:

```bash
trade account list
trade account use "MT5 Demo"
```

`account use` accepts the displayed label, broker account ID, or internal UUID and asks for
explicit confirmation. It writes `TRADING_WORKSPACE` and `TRADING_ACCOUNT` to the local
configuration for new sessions and queries. Restart `trade` after switching; an existing
session remains bound to its original account so its memory cannot silently change identity.
The list output prints the selected account's full internal UUID and account-specific
TradingView webhook path for copy/paste setup.

Broker setup registers a `trading_accounts` row in the configured workspace. The external
broker ID is unique only within that workspace/broker pair, allowing separate workspaces
without joining their histories. Every broker connection, cursor, execution, fill, snapshot,
and reconciliation relationship carries and validates the same workspace/account pair.

## 1. Broker facts

OANDA or the MetaTrader bridge provides live quotes/candles, executions, fills, costs,
account state, and net positions.

```bash
trade integrations
trade integrations --verify-live
trade broker configure-oanda --help
trade broker configure-metatrader --help
trade broker quote XAUUSD
trade broker sync
```

Quotes and candle windows are bounded and transient. Executions, fills, costs, account
snapshots, position snapshots, cursors, and reconciliation results are stored in PostgreSQL.
External event IDs make sync idempotent within the scoped connection. The connector is
read-only and cannot place, modify, cancel, close, or hedge an order.

The result includes cursor before/after, `has_more`, and an explicit history-coverage state.
Present-time baselines and cursor-based incremental history are partial: account/position
snapshots are stored, but they are not compared with a fill ledger that may begin after an
open position. Only proven complete coverage may produce a full quantity reconciliation.
If a provider reuses an external event ID with changed normalized content, the event is
skipped, the cursor is held, and the connection is degraded.

OANDA history reads use transaction-ID ranges of at most 1,000 IDs and response bodies are
byte-bounded. When `has_more` is true, rerun `trade broker sync` to advance the durable cursor.
Starting explicitly at transaction ID `0` can prove complete coverage only when the entire
history fits in one bounded page; a multi-page import remains conservatively incremental until
durable coverage tracking is added.

`trade integrations` separates code availability, configuration, prior/live connection
verification, and accepted real evidence. Use `--verify-live` only when you want bounded
read-only calls to configured providers; the check may consume API quota but does not store
account data, events, headlines, search results, or ticks. A provider failure is reported by
safe error type without echoing credentials, account IDs, response bodies, or exception
text.

## 2. News and calendar evidence

Set `NEWS_PROVIDER=forex-factory` for the free public weekly calendar feed, or select
`trading-economics` and configure its API key for calendar plus headline metadata. Both
preserve provider and retrieval timestamps:

```bash
trade news sync \
  --start 2026-07-26 \
  --end 2026-08-02 \
  --countries "United States,Euro Area,United Kingdom" \
  --minimum-importance 2
```

View a concise stored-event window without another provider request:

```bash
trade news upcoming --hours 24 --currencies USD,EUR --minimum-importance 2 --details
trade news history "Core PCE" --currency USD --limit 6
trade news watch --currencies USD,EUR --alert-minutes 60 --yes
```

Forex Factory provides calendar events, not a headline API. Its public export covers the
current week, so scheduled refreshes update that window and PostgreSQL remains the cache when
the source is temporarily unavailable. `news watch` runs until Ctrl-C, refreshes at a bounded
interval, and prints each newly due event once per running watcher.

`--details` shows stored actual/forecast/previous values plus a reviewed event definition,
common market-sensitivity channels, interpretation cautions, and an original
statistical-agency reference. The Forex Factory weekly export normally supplies forecast and
previous values but may leave actual unavailable; the CLI labels it `Pending` rather than
inventing a result. Unknown events remain explicitly unclassified instead of receiving a
generated definition.

Inside `trade chat`, traders can ask naturally: “Show me today’s economic news,” “Show only
high-impact events for the United States and Euro Area,” or “Show me the previous six Core PCE
releases.” Current-calendar requests default to every available country and impact level when
the trader does not narrow the request. Historical rows are retrieved only on explicit request.
Because the free feed covers the current week, its local release history grows as syncs are
retained and is not a complete historical archive.

The database retains metadata rather than copying full articles. The pre-trade workflow uses
the stored calendar to show a compact nearby-event reminder when the trader expresses explicit
near-term entry intent and the instrument maps to a relevant currency. The reminder does not
interrupt the conversation or decide whether to trade. News is evidence for conditional
scenarios, not proof of manipulation or a direction.

## 3. TradingView chart alerts

TradingView can POST alert events into an account-specific public HTTPS receiver. The app
stores the selected workspace/account with the normalized symbol, timeframe, trigger time,
optional OHLCV values, alert condition, verification method, and a payload hash. Duplicate
deliveries are idempotent.

This is chart evidence, not a broker feed. An alert cannot select a strategy, create an order,
or prove the broker's current price. OANDA/MT4/MT5 remains authoritative for live account and
execution state. See [the secure webhook setup](tradingview-webhooks.md).

TradingView is inbound-only, so the app does not fake a successful verification by POSTing to
itself. Enable the secured receiver and send a real TradingView test alert; the qualification
report then shows the last accepted delivery time.

## 4. Trader knowledge

Historical notes and social exports are retrieval material, not broker facts and not model
weight training. Create the strategy first, then import each source only into the exact
immutable version where it belongs:

```bash
trade knowledge import ~/Downloads/discord-package.zip --strategy wyckoff-pure
trade knowledge import ~/Downloads/Telegram\ Desktop/result.json --strategy wyckoff-pure
trade knowledge import ~/Downloads/x-archive/data/tweets.js --strategy ict-pure
trade knowledge import ~/Documents/trading-notes --strategy wyckoff-pure
trade knowledge paste --strategy wyckoff-pure --name "current execution rules"
```

Supported inputs include TXT, Markdown, JSON, JSONL, CSV, JavaScript data archives, ZIP
archives, and directories. Imports are size-bounded, content-hashed, deduplicated, treated as
untrusted text, and never allowed to invoke tools. Discord, Telegram, and X authors,
timestamps, message IDs, and source references are retained when present.

Keeping Wyckoff and ICT imports in separate strategy versions prevents retrieval from mixing
them. A combined method must be a third explicit strategy, not an accidental cross-query.
Quarantine a bad or irrelevant item instead of deleting its audit record.

## Model use

The model receives only bounded application queries:

- recent broker observations with source and market/retrieval timestamps;
- stored execution/journal aggregates scoped to the current workspace, account, and strategy;
- selected news/calendar records;
- search results from the one active immutable strategy version.

The account is host-selected before the model is called. The model has no tool for changing
it and no arbitrary SQL access. The same isolation applies to recall: switching accounts
changes the eligible profile, constraints, conversations, plans, tests, executions, and
mindset history for new sessions rather than blending records.

Imports do not silently fine-tune OpenAI, Anthropic, Qwen, or another model. Retrieval-first
use is reversible, immediately updateable, source-citable, and safer for private data.
Fine-tuning should come later only from a deliberately curated, consented, de-identified
training set with a frozen evaluation set.

## Verify what arrived

```bash
trade data status
trade data schema
trade strategy list
trade knowledge search "spring reclaim" --strategy wyckoff-pure
```

`trade data status` shows grouped row counts and latest durable timestamps. It will not show
every tick because ticks are intentionally not a PostgreSQL retention stream.
