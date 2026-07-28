# Trading Agent roadmap

## Status

This roadmap was originally researched in July 2026 and was realigned with the implemented
repository state on 2026-07-26. It is directional, not a release promise. Each future phase
requires its own design, threat model, tests, and trader approval.

The active product is a discretionary copilot. Autonomous execution remains a separate,
future program described in
[the autonomous-execution boundary](autonomous-execution-boundary.md).

## Product tracks

### Track A — discretionary copilot

The trader owns every order. The system gathers timestamped evidence, challenges a thesis,
calculates risk deterministically, records decisions, and evaluates immutable strategies.

### Track B — execution research

A separate service may eventually consume approved, versioned strategies and submit tightly
bounded orders. It cannot be created by adding an order tool to the conversational agent.
No work in this track begins past research or shadow mode until the prior promotion gate is
met.

## What exists now

### Interaction and provider layer

- Interactive Typer/Rich CLI with named sessions.
- Shared application services for CLI and FastAPI.
- OpenAI, Anthropic, and local Ollama provider adapters.
- Model routing, cost estimates, and memory-pressure-aware local-model selection.
- Isolated development handoff with reiterated scope and no broker/database credentials.

### Journal and evidence

- PostgreSQL plans, reviews, management events, normalized fills, and account snapshots.
- Human-readable trade and session references while retaining internal UUIDs.
- Content-addressed screenshot evidence and structured chart analysis.
- Separate direct observations and hypotheses.
- Source and retrieval-time reference ledger.

### Decision workflow

- Guided preflight for exact strategy rules, deterministic sizing, evidence completeness,
  mindset, predefined-risk acceptance, and news readiness.
- Auditable Eligible, Conditional, Stand Aside, and Blocked results.
- Human final decision and no broker submission.

### Strategy and learning

- Immutable strategy versions and exact strategy-scoped conversation history.
- Discord, Telegram, X, text, JSON, CSV, ZIP, and directory knowledge imports.
- Reversible natural-language quarantine and restoration of one exact knowledge item.
- Manual backtest and forward-test experiments with frozen rule hashes.
- Deterministic market-feature measurements and descriptive correlations.
- Experience-adjusted learning curricula that remain separate from execution rules.

### Read-only external truth

- OANDA quotes, candles, account state, transactions, and fills.
- Trading Economics calendar and news metadata.
- Allowlisted documented-page fetch and separately confirmed broad search.
- Tiered evidence with untrusted-content boundaries.

## Definition of an edge

A setup becomes an edge only after it has:

1. a frozen, versioned definition;
2. observable inclusion, exclusion, trigger, and invalidation rules;
3. a representative opportunity log including skipped and rejected trades;
4. costs, spread, slippage assumptions, and data provenance;
5. untouched out-of-sample results;
6. regime-specific sample sizes and uncertainty;
7. review for look-ahead, leakage, selection bias, and outlier dependence;
8. forward evidence showing that the measured behavior persists.

The model can propose a hypothesis. It cannot promote one to an edge.

## Near-term priorities

### 0. Stabilize the current baseline

Before implementing more product capabilities, turn the current broad passing change set into
an inspectable baseline that can be reviewed safely by people and coding agents.

Requirements:

- inventory all tracked and untracked changes without discarding or overwriting existing work;
- group the work into coherent review units such as scope/security, broker integrations,
  strategy and learning, interface workflows, release engineering, tests, and documentation;
- verify migration ordering and upgrade behavior before separating database changes;
- keep each proposed commit independently reviewable and passing where dependency order allows;
- run Ruff, the full test suite, strict health checks, and migration/release verification at
  the final consolidated baseline;
- record any intentionally deferred warnings or incomplete integrations instead of hiding them;
- establish a clean reviewed Git base before starting Hermes or another coding worker in a
  worktree;
- require human review of the proposed commit plan and diffs before creating commits.

This stabilization work does not remove features. It reduces change-management risk and gives
later refactors a trustworthy starting point.

### 1. Resumable trade-decision workflow

Unify the existing functions into one named lifecycle:

```text
pre-session
  → context and chart evidence
  → preflight
  → trade or no-trade decision
  → during-trade management
  → post-trade review
  → daily/weekly reflection
```

Requirements:

- checkpoint and resume without losing exact strategy scope;
- attach chart analysis and market/news evidence to the same decision;
- preserve every rule answer and human override;
- record no-trades and incomplete workflows;
- retain standalone commands as recovery tools;
- never convert an Eligible rating into broker authorization.

### 2. Product-surface consolidation

Make the resumable decision lifecycle the primary product experience instead of requiring
the trader to navigate many independent command groups. Preserve existing capabilities and
standalone commands as compatibility and recovery paths while presenting six coherent
user-facing areas:

```text
trade          resume the current trading workflow
trade prepare  account, strategy, mindset, news, and session readiness
trade analyze  chart, market evidence, and bounded research
trade decide   preflight, deterministic risk, plan, and trade/no-trade decision
trade manage   management events and read-only broker synchronization
trade review   post-trade, daily/weekly, journal, experiment, and edge review
trade admin    setup, integrations, models, database, credentials, and development
```

Requirements:

- organize the interface into daily, research, and administration modes without duplicating
  domain logic;
- have CLI, chat, API, and future MCP adapters call one policy-aware application-command
  boundary;
- show current lifecycle state and the next valid action instead of requiring command recall;
- keep direct commands available for expert use, automation, and interrupted-workflow recovery;
- split oversized CLI, agent, and model modules along stable product and domain boundaries
  through behavior-preserving refactors;
- add characterization tests before moving code and retain all current policy, confirmation,
  provenance, account-scope, immutable-strategy, deterministic-risk, and read-only-broker
  controls;
- freeze new provider, social-connector, agent-swarm, hosted-product, and execution features
  until the core lifecycle is complete and has been exercised in daily use;
- remove a compatibility path only after usage and tests show that it is redundant.

Completion means a trader can prepare, analyze, decide, manage, review, stop, and resume one
scoped session without understanding the underlying command inventory, while advanced and
administrative capabilities remain deliberately accessible.

Resource and delegation constraints:

- keep Trading Agent domain-specific; do not embed Hermes, OpenClaw, LangGraph, or another
  general-purpose agent harness as a runtime dependency;
- use external coding harnesses only as isolated development workers with reviewed diffs and
  no broker, journal, customer, live-position, or proprietary-strategy credentials;
- keep Codex or a human maintainer responsible for architecture, security review, integration,
  and final validation;
- select routine local-model profiles from detected available memory, swap pressure, model
  footprint, context requirements, currently loaded models, and a configurable system reserve
  rather than assuming a particular machine or model size;
- treat any local model near the configured resource boundary as an explicit, resource-guarded
  quality option rather than a continuously resident default, and route or refuse predictably
  when running it would impair the application, database, tests, or operating system;
- use a low-cost hosted coding worker for heavier nonsensitive implementation when local memory
  pressure would impair the application, database, tests, or development environment.

### 3. Unified market snapshot and data-quality gate

Before a trade can be rated Eligible, assemble one typed, timestamped decision snapshot from
the available broker, market, account, strategy, news, chart, and trader-state inputs. Broker
data is the authority for broker-specific facts; charts, alerts, news, and model analysis are
supporting evidence and must not silently replace missing broker truth.

The snapshot should include:

- fresh bid, ask, midpoint, spread, market timestamp, retrieval timestamp, source, and venue;
- complete multi-timeframe candle windows with explicitly labeled volume semantics;
- the exact versioned instrument contract used for tick size and value, contract size,
  quantity limits and increments, margin, commission, spread, slippage, and conversion;
- current account equity, available margin, open positions, related exposure, and
  reconciliation state when required by the selected strategy or account constraints;
- exact immutable strategy requirements, exclusions, trigger, invalidation, and evidence;
- relevant chart observations, TradingView alerts, economic events, news, session context,
  mindset, predefined-risk acceptance, and personal or prop-account constraints;
- a host-generated data-quality report that the model can explain but cannot override.

The data-quality report should evaluate:

- freshness, completeness, continuity, duplicates, ordering, and candle completeness;
- quote/candle consistency, clock skew, session or market status, and symbol/venue mapping;
- spread against a versioned recent baseline and any configured maximum;
- cross-source price or timestamp disagreement using explicit tolerances;
- instrument-contract age, currency-conversion age, margin availability, and provider health;
- missing required timeframes, feed outages, partial history, and ambiguous account exposure;
- provider-specific volume meaning and whether it represents broker activity, ticks, lots,
  exchange contracts, or is unknown;
- asset-specific concerns such as futures expiry and rollover, open interest, corporate
  actions, and adjusted versus unadjusted prices when applicable.

Decision behavior must be deterministic and strategy-aware:

- healthy required data permits strategy evaluation but never guarantees a trade;
- missing nonessential supporting evidence produces a visible warning or Conditional result;
- missing required strategy evidence produces Conditional or Stand Aside;
- stale executable quotes, invalid or expired contract data, unknown required margin,
  material cross-source conflicts, broken feeds, or unresolved exposure produce Blocked;
- unsupported or ambiguous volume is labeled limited or unavailable and is never presented as
  centralized or institutional volume;
- thresholds, required sources, and severity mappings are versioned configuration rather than
  model judgment;
- every result records the complete snapshot, quality findings, source timestamps, applicable
  thresholds, and reasons for any downgrade or block.

For decentralized spot FX and broker CFDs, broker candle volume must be described as
provider-observed activity or tick volume unless the provider contract proves otherwise. Actual
exchange contract volume, open interest, and order-book claims require an appropriate
exchange/futures data source.

Completion means an Eligible preflight proves that all strategy-required data was assembled,
fresh enough, internally valid, and cross-checked under the recorded policy. It does not mean
the setup will win, constitute advice, or authorize broker execution.

### 4. Numeric bridge from visual concepts to evidence

- Flatten deterministic candle measurements into experiment-ready numeric fields.
- Version imbalance, equal-level, sweep, displacement, and range definitions.
- Record feature counts, magnitude, distance, age, session, and timeframe.
- Join event proximity, actual/forecast surprise, volatility, USD, and yield context where
  licensed and available.
- Keep news association descriptive; never label unexplained movement “manipulation.”
- Preserve raw source and as-of timestamps for every derived value.

### 5. Daily and weekly desk outlook

- Multi-timeframe OANDA evidence rather than a single candle window.
- Relevant upcoming events with actual, forecast, previous, and source timestamps.
- Allowlisted primary macro sources before broad search.
- Separate measured facts, sourced macro context, conditional strategy scenarios,
  invalidation, and missing evidence.
- Persist an outlook run card so later review uses what was known at the time.
- Cite every external claim.

### 6. Replay and forward-test engine

The current experiment ledger accepts manually entered samples. The next research layer
should add:

- event-time replay clock;
- frozen dataset, strategy, parameters, and configuration run card;
- opportunity sampling that retains excluded and unclear examples;
- discovery versus untouched evaluation splits;
- walk-forward and purged/embargoed evaluation where samples overlap;
- bootstrap intervals and Monte Carlo trade-order analysis;
- multiple-testing correction;
- spread, slippage, fees, latency, rejection, and rollover;
- look-ahead and recursive-indicator diagnostics;
- comparison with simple baselines and publication of negative results.

No model-generated Python executes directly.

### 7. Security consolidation

- Keep all external text in explicit untrusted-content envelopes.
- Centralize semantic mutations behind one policy-aware command boundary.
- Preserve exact host confirmation for database and behavior changes.
- Treat routine append-only audit logging as a documented exception, not a hidden mutation.
- Add adversarial provider, imported-content, chart-label, and web-injection fixtures.
- Continue dependency auditing, secret scanning, path constraints, SSRF controls, and
  least-privilege database roles.

### 8. Interface adapters

- Expose bounded journal, risk, chart, strategy, and review capabilities through MCP.
- Reuse the exact runtime policy metadata and confirmation hooks.
- Add request IDs, timeouts, audit traces, and per-client strategy scope.
- Consider Discord only after the core workflow is resumable and secure.
- Keep Discord credentials and message transport outside domain services.

## Execution research phases

Execution work remains gated and separate.

### Historical and shadow

- Operational definitions are stable.
- Frozen strategy passes untouched evaluation with costs and uncertainty.
- Shadow intents run against live timestamped data without submission.
- Trader and agent decisions are compared by setup, regime, and timeframe.

### Paper execution foundation

- Separate process, package, credentials, and database role.
- Typed `TradeIntent`, `RiskDecision`, `OrderCandidate`, `BrokerOrder`, `Fill`, and
  `Position`.
- Deterministic risk gate and idempotent client-order IDs.
- Broker simulator with spread, slippage, latency, rejection, partial fills, margin, and
  disconnects.
- Persistent order state, restart recovery, reconciliation, and fault injection.
- STOPPED, RUNNING, PAUSED, DEGRADED, and HALTED states.
- Independent kill switch.
- Human approval for every paper submission during this phase.

### Supervised micro-live and bounded autonomy

These phases are not scheduled. They require every gate in the execution-boundary document:

1. smallest broker-supported risk;
2. intent-bound expiring approval for every order;
3. independent reconciliation and monitoring;
4. narrowly allowlisted strategy, symbol, session, order type, and size;
5. automatic regression to an earlier gate after any policy, model, prompt, provider, or
   adapter change.

## Architecture decisions

- PostgreSQL remains the canonical durable ledger.
- Continuous ticks remain in bounded memory/cache, not PostgreSQL.
- The CLI remains the fastest local interaction loop.
- API and future MCP reuse domain services rather than duplicating logic.
- Market, news, and broker adapters preserve provider-specific provenance.
- Model providers remain optional adapters behind one protocol.
- Strategy education does not change execution rules.
- Capture consistent discretionary decisions before adding agent swarms.
- Treat autonomous execution as an independent security and operations program.

## Explicit non-goals

- No automatic broker order placement, modification, cancellation, hedging, or liquidation
  in the conversational product.
- No model-selected authoritative position size.
- No strategy promotion from model confidence, GitHub stars, screenshots, or a small journal.
- No arbitrary model-generated code execution in analysis or replay.
- No silent data-provider fallback.
- No automatic self-training from private chats or unreviewed imported material.
- No claim that Wyckoff or ICT/SMC labels are facts before operational definition and review.

## Research basis

The project-by-project findings and links behind these decisions are in
[the July 2026 landscape review](research/trading-agent-landscape-2026-07.md).
