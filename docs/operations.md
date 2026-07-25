# Operations

## Startup

```bash
source .venv/bin/activate
trading-agent health --strict
trading-agent
```

The CLI talks directly to Python services; `trading-agent api` is optional. Startup loads
and hashes the runtime rules, checks PostgreSQL and migration state, checks configured
providers/connectors, and resumes the latest named conversation unless told otherwise.

## Model routing

`AGENT_MODE=auto` classifies each message locally before any provider request. Routine
journal operations use economy effort, normal analysis uses balanced effort, and broad
research uses deep effort. In chat, use `/mode` to override this for the rest of that
session. The route and model are printed with every reply.

All three routes fall back to the provider's base model. Set the matching
`OPENAI_*_MODEL` or `ANTHROPIC_*_MODEL` values only when you intentionally want separate
models. Risk and position sizing never use this router; they remain deterministic.

## Development handoff

Set `DEVELOPMENT_REPOSITORY` to the absolute clone path when the agent may start outside
the repository. Confirm Codex is installed and signed in:

```bash
codex --version
codex login status
trading-agent health
```

In interactive chat, state a clear software change or use `/develop`. After the scope
confirmation, the command may run for several minutes. It records a local session under
`.data/development`, creates an `agent/dev-*` branch in a separate worktree, runs Codex
non-interactively with workspace-only writes, then runs Ruff and pytest.

Inspect and commit the result:

```bash
trading-agent develop status SESSION_ID
trading-agent develop diff SESSION_ID
trading-agent develop approve SESSION_ID --yes
```

Approval reruns validation and commits only to the isolated branch. Push, pull-request,
merge, deployment, and process restart are deliberately separate operations.

`DEVELOPMENT_APPROVAL_FLOW=scope_only` removes the second commit approval: the initial
reiterated-scope confirmation authorizes Codex to edit, validate, and commit on the isolated
branch. `scope_and_diff` is the default for shared installations.

## Database

Inspect and upgrade:

```bash
trading-agent db status
trading-agent db upgrade
```

If a pre-Alembic database is detected, normal upgrade fails closed. Adopt it only after
choosing a new absolute backup path:

```bash
trading-agent db adopt-legacy \
  --backup /absolute/safe/path/trading-agent-before-adoption.dump \
  --yes
```

The command creates a restricted `pg_dump`, transactionally moves the legacy tables,
applies migrations, copies rows, verifies counts, and removes the temporary legacy schema
only after verification succeeds.

## OANDA read-only sync

Put `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, and `OANDA_ENVIRONMENT=practice` in `.env`.
Then:

```bash
trading-agent broker configure-oanda --label practice --currency USD
trading-agent broker quote XAU_USD
trading-agent broker sync
```

The first sync intentionally starts from OANDA's current transaction cursor instead of
silently importing an unbounded history. To import history on a new connection, explicitly
provide the transaction immediately before the desired range:

```bash
trading-agent broker sync --from-transaction-id 12345
```

Once a cursor exists, rewind is refused. Transaction imports are idempotent. Every sync
also records account/position snapshots and compares broker positions with the normalized
fill ledger. A mismatch marks the connection degraded.

## Instrument contract and sizing

Create a JSON instrument specification from the broker's current contract details, then:

```bash
trading-agent instrument configure --file examples/oanda-xauusd-spec.json
trading-agent instrument risk --help
```

Sizing includes stop distance, spread, slippage, round-trip commission, quantity step,
minimum/maximum quantity, margin, conversion rate, and the configured maximum risk. The
stored specification is versioned, so an old plan keeps the assumptions used at the time.

## News and calendar

Set `TRADING_ECONOMICS_API_KEY`, then:

```bash
trading-agent news sync --help
```

Only provider metadata and summaries are retained: external ID, original timestamps,
retrieval timestamp, importance, country/category/symbol, values, and source URL. Provider
entitlements and redistribution terms still apply.

## Evidence and analytics

Chart analysis stores the original image by SHA-256, provider/model, policy/prompt/input/
output hashes, output JSON, and normalized facts versus hypotheses.

```bash
trading-agent chart /absolute/path/chart.png \
  --instrument XAUUSD --venue OANDA --timeframe M5
trading-agent edge report --minimum-sample 30
```

Expectancy remains explicitly unvalidated below the minimum sample. Process score and
outcome are kept separate.

## Recovery

- Keep `.env` and backups outside Git.
- Test restores periodically with `pg_restore` into a separate database.
- If reconciliation becomes degraded, stop relying on derived position state, verify the
  configured account and cursor, and compare broker transactions against imported fills.
- Evidence is file-backed. Back up both PostgreSQL and `.data/evidence` to preserve a
  complete audit trail.
