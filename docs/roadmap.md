# Trading Agent roadmap

## Product tracks

### Track A — discretionary copilot (active)

The trader owns every order decision. The system gathers evidence, challenges a thesis,
calculates risk, records actions, and evaluates the playbook.

### Track B — autonomous execution (future, separately gated)

A separate service may eventually consume approved, versioned strategies and submit tightly
bounded orders. It is not enabled by adding a broker tool to the chat agent. See
[the execution boundary](autonomous-execution-boundary.md).

## Definition of an edge

A named setup becomes an edge only when it has:

1. a frozen, versioned definition;
2. observable inclusion, exclusion, trigger, and invalidation rules;
3. a representative opportunity log including skipped trades;
4. costs, spread, slippage assumptions, and data provenance;
5. untouched out-of-sample results;
6. regime-specific sample sizes and uncertainty;
7. a review showing the result is not driven by look-ahead, leakage, or a few outliers.

The model can propose a hypothesis. It cannot promote one to an edge.

## Planned pull requests

### PR 2 — interactive CLI and shared application services

Goal: make the current MVP feel like an active terminal agent without duplicating business
logic.

- Add a Typer/Rich CLI entry point. Running `trading-agent` performs lightweight startup
  health checks and opens the interactive agent; `chat` remains an explicit alias.
- Add `plan`, `chart`, `journal`, `review`, `health`, and optional `api` commands.
- Keep CLI domain calls in-process. Do not require an HTTP server for normal terminal use.
- Reserve `api` for the long-running FastAPI process needed by the browser UI, hosted
  deployments, Discord/webhooks, or remote integrations.
- Accept screenshot paths from the CLI.
- Persist conversational sessions and link them to plans/trades.
- Move domain operations behind services shared by CLI and FastAPI.
- Stream progress without exposing hidden reasoning.
- Add redacted diagnostics and a resolved-configuration view.
- Make startup checks non-destructive and fast; direct users to `trading-agent health` for
  the full database, configuration, model, and provider diagnostic report.
- Test CLI exit codes, missing configuration, image errors, and database failures.

Acceptance boundary: all commands remain decision support; no broker mutation exists.

### PR 3 — evidence ledger, operational playbook, and review loop

Goal: create reliable data from discretionary decisions before attempting strategy discovery.

- Add evidence records with source, venue, instrument, timeframe, observed-at, retrieved-at,
  attachment hash, and quality flags.
- Add decision records for taken trades, skipped trades, rejected plans, and overrides.
- Version the setup taxonomy and checklist.
- Operationalize equal highs/lows, swing points, displacement, imbalance, POI freshness,
  liquidity sweep, BOS, and the “80% of accumulation” calculation.
- Add separate context, trigger, invalidation, target, management, and hedge fields.
- Record planned/actual risk, partials, runner state, MFE, MAE, fees, spread, and slippage.
- Add pre-trade recall and post-trade/day/week reflection.
- Decompose conviction, evidence completeness, data quality, and statistical edge estimate.
- Import the documented XAUUSD example as a reviewed fixture with no private account data.

Acceptance boundary: a reviewer can reproduce why a plan qualified using only as-of evidence.

### PR 4 — MCP and interface adapters

Goal: use the same safe capabilities from Codex/Claude-compatible MCP clients and later
Discord.

- Expose journal, recall, deterministic risk, chart analysis, and review as MCP tools.
- Mark every tool with read/write capability metadata.
- Require explicit confirmation for journal mutations from conversational clients.
- Add session IDs, request IDs, timeouts, and audit traces.
- Add a Discord adapter boundary for slash commands and image uploads.
- Keep Discord credentials and domain logic outside the core.
- Document local CLI, MCP client, API, and Discord setup.

Acceptance boundary: MCP and Discord call shared application services and expose no broker
order tool.

### PR 5 — market data, news, and read-only broker adapters

Goal: ground decisions in timestamped external truth.

- Define provider protocols for XAUUSD bars/quotes, economic calendar, news, and broker reads.
- Create canonical schemas with provider/venue/symbol/timezone provenance.
- Implement one primary and one test/fallback provider per critical data class.
- Surface fallback and staleness in every response.
- Cache immutable as-of snapshots and hash raw evidence.
- Add event-time/news-time semantics and macro blackout metadata.
- Add read-only positions, orders, fills, account equity, and symbol specifications.
- Reconcile journal claims against broker fills without silently overwriting either.
- Add contract tests with recorded, sanitized fixtures.

Acceptance boundary: stale, missing, ambiguous, or conflicting data cannot become an
unqualified numeric claim.

### PR 6 — replay, edge research, and regime evaluation

Goal: determine whether the operationalized playbook has evidence of repeatable expectancy.

- Build an event-time replay clock.
- Freeze dataset, playbook version, parameters, and configuration in a run card.
- Split hypothesis discovery from untouched evaluation data.
- Measure expectancy in R, win rate, payoff, drawdown, MFE/MAE, and execution variance.
- Segment XAUUSD by session, news proximity, volatility, trend/rotation regime, context/trigger
  timeframe, and setup version.
- Add walk-forward evaluation, bootstrap intervals, Monte Carlo trade-order analysis,
  multiple-testing correction, and purged/embargoed splits where samples overlap.
- Add look-ahead and recursive-indicator diagnostics.
- Compare against simple baselines and record negative results.
- Generate candidate playbook changes but require explicit human promotion.

Acceptance boundary: reports expose denominators, uncertainty, costs, and failed tests; no
strategy is labeled an edge based only on an in-sample result.

### PR 7 — supervised paper-execution foundation

Goal: create execution semantics without risking capital.

- Create the separate execution service/package and database role.
- Define typed `TradeIntent`, `RiskDecision`, `OrderCandidate`, `BrokerOrder`, `Fill`, and
  `Position` records.
- Add a deterministic risk gate, idempotent client order IDs, and immutable audit events.
- Add a broker simulator with configurable spread, slippage, latency, rejection, partial
  fills, margin, and disconnects.
- Add order-state reconciliation and restart recovery.
- Add STOPPED/RUNNING/PAUSED/DEGRADED/HALTED states and an out-of-process kill switch.
- Require a human approval token for any paper submission at this stage.
- Keep model credentials and broker credentials in different processes.

Acceptance boundary: fault-injection tests demonstrate duplicate prevention, fail-closed
behavior, recovery, and kill-switch control.

### Later PRs — shadow, supervised micro-live, bounded autonomy

These PRs are intentionally not scheduled until the preceding evidence exists:

1. **Shadow:** generate intents against live data but submit nothing; compare hypothetical
   decisions and fills with market truth.
2. **Paper:** use the real adapter surface with simulated capital and adversarial faults.
3. **Supervised micro-live:** smallest allowable risk, every order explicitly approved.
4. **Bounded autonomous:** only a frozen, approved strategy; narrow symbol/session/size;
   independent monitoring and kill switch.

Promotion is based on documented criteria in the execution-boundary document, not elapsed
time or backtest excitement.

## Priority decisions

- PostgreSQL remains the system of record. Neon is suitable for development if private
  journal and account data are protected with authentication and row-level isolation.
- Build the CLI before Discord; it gives the fastest interactive loop and keeps debugging
  local.
- Add MCP after services are shared, so Codex, Claude, and other clients use the same rules.
- Capture consistent discretionary data before adding broad market-data or agent swarms.
- Keep news/market adapters read-only and keep raw sources inspectable.
- Treat live autonomous execution as its own security and operational program.

## Explicit non-goals for the next PRs

- No automatic broker order placement, modification, cancellation, hedging, or liquidation.
- No model-selected position size.
- No strategy promotion from model confidence, star counts, screenshots, or a small journal.
- No arbitrary model-generated Python execution.
- No silent data-provider fallback.
- No claim that Wyckoff/SMC labels are facts before operational definition and review.
