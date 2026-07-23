# Trading-agent landscape review — July 2026

## Decision summary

The best product for this repository is not a clone of any one project. It should combine:

- **Vibe-Trading's interaction model:** one core service used through CLI, API, and MCP.
- **TradeMemory's discipline loop:** recall before a decision, record decisions including
  no-trades, reflect after outcomes, and retain an auditable history.
- **TradingAgents' workflow structure:** explicit, typed stages with bounded transitions,
  checkpoints, and deterministic market-data verification.
- **ai-hedge-fund's safety envelope:** code determines allowed actions and size; a model may
  reason only inside those limits and failure defaults to no action.
- **OpenBB's data contracts:** normalize multiple providers behind typed adapters while
  preserving provenance.
- **Freqtrade's operations:** explicit run modes, default stopped state, configuration
  validation, protections, reconciliation, and look-ahead testing.
- **Hummingbot's execution architecture:** separate strategy/controller logic from order
  executors, budgets, order lifecycle tracking, clocks, and restart recovery.

For Kyle's current workflow, the model remains a **discretionary copilot**. It structures a
Wyckoff/SMC thesis, challenges it, calculates risk deterministically, records the plan and
management decisions, analyzes screenshots, and later measures whether the named setup has
an edge. It does not place orders.

A future autonomous system should be a **separate deployable execution service**. The
language model may propose a typed `TradeIntent`; it must never receive unrestricted broker
tools or determine final size. A deterministic risk gate and execution engine must be able
to reject every intent without model involvement.

## Method

This review inspected each project's README, license, architecture, configuration, core
agent or strategy loop, persistence, validation, and order boundary. Popularity figures are
a snapshot observed on 2026-07-23 and are included only as an adoption/maintenance signal.
Stars do not demonstrate safety or profitability.

| Project | Stars | License | Strongest lesson for us | Direct fit |
| --- | ---: | --- | --- | --- |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 94.3k | Apache-2.0 | Typed, staged reasoning graph | Medium |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 70.9k | AGPL-3.0 | Provider-neutral data contracts | Medium |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 62.4k | MIT | Deterministic action envelope | Medium |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 52.6k | GPL-3.0 | Live-bot operational safety | High for future execution |
| [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | 26.9k | MIT | CLI/API/MCP copilot experience | High |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 19.2k | Apache-2.0 | Order lifecycle and executor separation | High for future execution |
| [TradeMemory](https://github.com/mnemox-ai/tradememory-protocol) | 1.4k | MIT | Decision memory and reflection | High |

License note: architecture ideas are not code. We should implement the selected patterns
independently. In particular, do not copy GPL/AGPL implementation code into this project
without making a deliberate licensing decision.

## Evaluation criteria

Each project was evaluated against the same needs:

1. **Evidence integrity:** timestamps, sources, as-of behavior, and no invented prices.
2. **Decision quality:** clear context, trigger, invalidation, alternatives, and uncertainty.
3. **Memory:** plans, no-trades, fills, management, outcomes, and later reflection.
4. **Evaluation:** replay, walk-forward testing, regime segmentation, and bias controls.
5. **Risk containment:** deterministic sizing, maximum loss, fail-closed behavior.
6. **Execution safety:** idempotency, order states, reconciliation, recovery, and kill switch.
7. **Interaction:** practical CLI, image input, MCP/API reuse, and inspectable traces.
8. **Fit:** discretionary intraday XAUUSD rather than equities research or crypto market making.

## 1. TradingAgents

### What it actually is

TradingAgents builds a LangGraph workflow for equity research. Market, sentiment, news,
and fundamentals analysts feed a bull/bear research debate. A research manager passes a
conclusion to a trader; aggressive, conservative, and neutral risk roles debate it; a
portfolio manager produces the final rating. The graph bounds debate rounds and supports
checkpointing. See its
[graph setup](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/graph/setup.py),
[conditional routing](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/graph/conditional_logic.py),
and [CLI package](https://github.com/TauricResearch/TradingAgents/tree/main/cli).

The project also records decisions and outcomes, then recalls same-symbol and cross-symbol
lessons. Its tests explicitly cover no-data behavior, look-ahead risk, stale data, provider
routing, structured outputs, and checkpoint resume.

### Take

- A typed state object passed through named stages.
- A bounded state machine, not an unconstrained chat loop.
- Checkpoint/resume so a pre-market plan can become an in-session review and later a
  post-trade reflection.
- Structured output with validation and a conservative fallback.
- A deterministic market snapshot that verifies numeric claims before the model sees them.
- Delayed reflection after the outcome is known.

### Adapt

Replace its equity pipeline and persona debate with evidence stages that match this playbook:

`context evidence → competing scenarios → trigger evidence → deterministic risk →
management plan → journal → outcome reflection`

One role should actively search for disconfirming evidence. “Bull,” “bear,” and “risk”
agents using the same model and source data are correlated opinions, not independent
confirmation. Record the evidence behind each conclusion rather than counting model votes.

### Reject or defer

- Famous-role or personality theater.
- A coarse Buy/Hold/Sell rating as the primary output.
- Multi-agent discussion on every request; it adds latency and cost without necessarily
  adding information.
- Markdown files as the authoritative memory store. PostgreSQL should remain canonical.

### Fit for XAUUSD

Use the graph pattern for a single plan across 4h/1h context and 15m/5m/1m trigger evidence.
The graph must prevent lowering the timeframe from silently creating a new thesis after the
original invalidates.

## 2. OpenBB

### What it actually is

OpenBB is primarily a financial-data platform, not a trading decision agent. Its provider
extensions implement a consistent lifecycle—query transformation, extraction, and result
transformation—then normalize provider-specific responses into standard models. The
[Fetcher interface](https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/abstract/fetcher.py),
[provider registry](https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/registry.py),
and [query executor](https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/query_executor.py)
are the important pieces.

It exposes data through Python, REST, CLI, and MCP-oriented integrations. Its repository
license is [AGPL-3.0](https://github.com/OpenBB-finance/OpenBB/blob/develop/LICENSE).

### Take

- A small provider protocol with typed query and result models.
- A canonical schema independent of the data vendor.
- Provider capability discovery and explicit required credentials.
- Provenance on every observation: provider, instrument, venue, timeframe, market timestamp,
  retrieval timestamp, and raw-response hash.
- Contract tests that run against every provider adapter.

### Adapt

Our first data contract should be narrower than OpenBB:

- XAUUSD OHLCV and bid/ask snapshots.
- Economic-calendar events that materially affect gold or USD.
- Timestamped news headlines and source links.
- Later, read-only positions, orders, and fills.

Fallbacks must be visible. The agent should say that a source changed, not silently splice
two vendors into one apparent series. OpenBB may be a useful macro/news provider, but its
XAUUSD intraday latency and venue semantics must be validated before relying on it.

### Reject or defer

- Installing a broad financial platform before the first two provider contracts are stable.
- Copying AGPL implementation code into this repository.
- Treating normalized fields as equivalent when vendors use different sessions, symbols,
  adjustments, or timestamps.

## 3. ai-hedge-fund

### What it actually is

This educational project simulates an equity hedge fund using many investor personas plus
fundamentals, technicals, sentiment, valuation, risk, and portfolio roles. Its README says
that it does not currently place real trades. The valuable implementation is its
[risk manager](https://github.com/virattt/ai-hedge-fund/blob/main/src/agents/risk_manager.py),
[portfolio manager](https://github.com/virattt/ai-hedge-fund/blob/main/src/agents/portfolio_manager.py),
and V2 [broker protocol](https://github.com/virattt/ai-hedge-fund/blob/main/v2/brokers/protocol.py).

The risk layer computes volatility/correlation constraints and passes the portfolio model an
allowed action set and maximum quantities. If reasoning or parsing fails, the safe result is
HOLD. Its simulated broker has a clean interface but deliberately omits partial fills,
slippage, and margin.

### Take

- Compute the allowed action set before asking a model what to do.
- Keep risk math outside prompts.
- Default to no action when data, parsing, or a dependency fails.
- Define a broker protocol early so simulation, paper, and live adapters share semantics.
- Distinguish intent, submitted order, fill, and position.

### Adapt

For the current copilot, the envelope is advisory:

```text
allowed: [NO_TRADE, PLAN_LONG, PLAN_SHORT]
max_risk_usd: deterministic result
required_invalidation: price level plus reason
```

For future automation, a model can propose only a `TradeIntent` inside this envelope. The
risk gate recalculates exposure from broker truth immediately before submission.

### Reject or defer

- Investor personas as evidence.
- A simplistic simulator for promotion to live capital.
- Model-generated confidence as a position-sizing input.
- Equity-specific fundamentals as the center of an XAUUSD intraday workflow.

## 4. Freqtrade

### What it actually is

Freqtrade is a mature autonomous crypto bot with dry-run/live modes, backtesting,
configuration validation, persistence, protections, exchange adapters, and operational
control through UI/Telegram. Its documentation says to start in dry-run. Relevant sources
include [configuration validation](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/configuration/config_validation.py),
[bot startup](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/freqtradebot.py),
and its [look-ahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/).

It validates incompatible or unsafe settings at startup, supports a stopped initial state,
keeps credentials away from shared configuration, persists operational state, locks pairs,
and exposes protections. It also provides backtest diagnostics for look-ahead and recursive
indicator errors.

### Take

- Make `research`, `paper`, and `live` explicit run modes; default to `research` or stopped.
- Validate configuration and cross-field invariants before starting.
- Add a command that shows the fully resolved, redacted configuration.
- Keep secrets separate and redact them from logs and model context.
- Symbol/session allowlists, cooldowns, circuit breakers, and a global kill switch.
- Startup reconciliation against broker truth and persistence of the order lifecycle.
- Look-ahead-bias tests, clock discipline, and latency/stale-data warnings.
- An operator-visible state machine: `STOPPED`, `RUNNING`, `PAUSED`, `DEGRADED`, `HALTED`.

### Adapt

These are future execution-plane requirements, not reasons to add crypto-specific bot code
to the copilot. Apply the same rigor to XAUUSD venue hours, spreads, rollover, macro-event
blackouts, margin, and broker-specific symbol properties.

### Reject or defer

- Crypto exchange and pair-selection machinery.
- Auto-optimization as proof of edge; it can amplify overfitting.
- Any assumption that a backtest setting transfers unchanged to live trading.
- Copying GPL-licensed code.

## 5. Vibe-Trading

### What it actually is

Vibe-Trading is the closest product benchmark: a FastAPI/React application with an
interactive CLI/TUI, MCP server, journal, market-data fallbacks, backtest engines, and
agent/swarm workflows. Its
[agent skill](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/SKILL.md) describes a
shadow loop that analyzes the journal, extracts if/then rules, backtests them, and scans for
signals. Its [MCP server](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/mcp_server.py)
keeps trading-connector tools read-only.

Its agent loop adds persistent sessions, context compaction, parallel read-only tools,
timeouts, heartbeat, tracing, and redaction. Backtest validation includes Monte Carlo trade
reordering, bootstrap Sharpe confidence intervals, and walk-forward analysis. Run cards
capture configuration hashes, data sources, warnings, and artifacts.

### Take

- One domain core shared by CLI, HTTP API, and MCP.
- Natural-language routes for journal, chart, review, and shadow/replay workflows.
- Persistent sessions and resumable runs.
- A capability registry that identifies read-only versus mutating tools.
- Tool-call traces, timeouts, usage records, heartbeat, and redaction.
- Reproducible run cards: strategy version, configuration hash, dataset identity, source
  timestamps, warnings, and output artifacts.
- Walk-forward, bootstrap, and Monte Carlo evaluation.
- A read-only broker surface before any execution tool exists.

### Adapt

Start with a compact command surface:

```text
trading-agent chat
trading-agent plan
trading-agent chart <image>
trading-agent journal add
trading-agent review <trade-id>
trading-agent replay <dataset>
trading-agent doctor
trading-agent serve
```

The journal-to-rule loop should generate **candidate hypotheses**, not “profitable rules.”
Every hypothesis retains its discovery sample and must be evaluated on later, untouched
data split by regime.

### Reject or defer

- Dozens of swarms and skills before the core loop is measurable.
- Silent provider fallback without surfaced provenance.
- Locally executing arbitrary model-generated backtest code.
- Treating a young, fast-moving project's feature breadth as production validation.

## 6. Hummingbot

### What it actually is

Hummingbot is a mature automated crypto execution framework. Strategy V2 separates
controllers, which create actions from market logic, from executors that manage position,
grid, DCA, arbitrage, TWAP, order, and liquidity-provision lifecycles. See its
[controller base](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/controllers/controller_base.py),
[executor orchestrator](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/executors/executor_orchestrator.py),
and [budget checker](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/connector/budget_checker.py).

The budget checker adjusts or rejects order candidates based on available collateral while
reserving hypothetical collateral across a batch. Client order trackers maintain in-flight
state. Executor state is persisted and restored. The framework distinguishes realtime and
backtest clocks and supports a manual kill switch and paper exchange.

### Take

- `TradeIntent → OrderCandidate → RiskDecision → BrokerOrder → Fill → Position`.
- Stable client order IDs and idempotent submission.
- Reserve budget across concurrent candidates.
- Persist and reconcile every order transition.
- Separate controller decisions from executor mechanics.
- Use a clock abstraction so replay cannot accidentally read wall-clock time.
- Restart recovery and explicit handling of unknown broker state.
- Manual kill switch independent of the model process.

### Adapt

Use these patterns only in the future execution service. A discretionary XAUUSD copilot
does not need high-frequency connector machinery, but it does need the same semantic
separation when reading open positions and fills.

### Reject or defer

- Market-making, DEX/wallet, and crypto connector complexity.
- Assuming paper fills reproduce XAUUSD spread, slippage, rejection, or rollover.
- Sharing broker credentials with the conversational process.

## 7. TradeMemory Protocol

### What it actually is

TradeMemory is a local MCP/REST memory and governance layer. It records decisions, recalls
prior evidence using outcome/context/recency/confidence factors, reflects on results, and
maintains hashed audit records. It explicitly does not execute trades. See its
[architecture](https://github.com/mnemox-ai/tradememory-protocol/blob/master/docs/ARCHITECTURE.md),
[MCP server](https://github.com/mnemox-ai/tradememory-protocol/blob/master/src/tradememory/mcp_server.py),
and candid [limitations](https://github.com/mnemox-ai/tradememory-protocol/blob/master/LIMITATIONS.md).

Its strongest idea is that the system records decisions not to trade, not only fills. It
supports pre-trade recall, plans, affective/behavioral state, daily reflection, strategy
validation, and a chained SHA-256/Merkle audit structure. Its “legitimacy” gate reduces or
skips action when the memory sample or regime match is weak.

The project also publishes an important negative result: its LLM-generated strategies have
not graduated through its Deflated Sharpe Ratio, walk-forward, regime, and combinatorial
purged cross-validation gates. The maintainers correctly describe this as selection-bias
protection, not a reason to loosen the tests. See
[ADR 004](https://github.com/mnemox-ai/tradememory-protocol/blob/master/docs/adr/004-evolution-statistical-gates.md).

### Take

- Recall relevant prior plans **before** a new decision.
- Record no-trades, rejected intents, overrides, and management changes.
- Separate observation, decision, outcome, and reflection timestamps.
- Store setup and playbook versions with each decision.
- Promote memories from examples to beliefs only after sample-size and drift checks.
- Use drawdown/streak/behavior warnings as review prompts, never as model psychology facts.
- Add tamper-evident hashes when the execution plane exists.
- Require multiple-testing-aware, out-of-sample gates for candidate edges.

### Adapt

Use PostgreSQL rather than its current SQLite-first split because this project already needs
concurrent API, CLI, MCP, Discord, and eventual worker access. Use structured rows plus
optional embeddings; never let semantic similarity erase exact filters such as instrument,
session, timeframe, playbook version, and as-of date.

“Confidence” must be decomposed:

- `trader_conviction`: self-report, never used to enlarge risk.
- `evidence_completeness`: deterministic checklist coverage.
- `edge_estimate`: statistical estimate with sample size and uncertainty.
- `data_quality`: provider freshness and integrity.

### Reject or defer

- A hand-authored 0–1 confidence value as a risk multiplier.
- Claiming an immutable audit trail when the same administrator controls data and hashes.
- Automatic belief induction from a small, selected journal.
- Its parallel SQLite/PostgreSQL stacks; this repository should have one canonical path.

## What combines well—and what does not

| Need | Primary inspiration | Our implementation |
| --- | --- | --- |
| Interactive use | Vibe-Trading | Typer/Rich CLI plus image input; same application services as API |
| Agent workflow | TradingAgents | Typed, bounded stages with checkpoints; no persona voting |
| Journal/memory | TradeMemory | PostgreSQL decisions, no-trades, outcomes, reflections, exact filters |
| Data | OpenBB | Narrow provider protocols and canonical evidence schema |
| Risk envelope | ai-hedge-fund | Deterministic allowed actions and maximum risk; fail to no-trade |
| Evaluation | TradeMemory + Vibe-Trading + Freqtrade | Walk-forward, multiple-testing correction, regime splits, bias diagnostics |
| Operations | Freqtrade | Run modes, STOPPED default, protections, reconciliation, resolved config |
| Execution | Hummingbot | Separate controller/executor, budget reservation, order state machine |

Do not combine agent counts, persona debates, broad data dependencies, backtest generators,
and live broker access into one process. That produces a large attack surface and makes it
impossible to tell whether a result came from evidence, a prompt, a fallback provider, or
an execution-side mutation.

## Recommended product boundary

The near-term agent should answer:

- What is visible on the chart, and what remains an interpretation?
- What higher-timeframe condition makes this lower-timeframe setup relevant?
- What would falsify the thesis?
- Does the proposed entry meet the versioned checklist?
- What size corresponds to the predefined risk using broker/venue specifications?
- What similar trades occurred under the same regime and setup version?
- Was the result due to setup quality, execution, management, or outcome variance?

It should not answer with fabricated certainty, infer live price from an old screenshot,
change rules after seeing the outcome, or place/modify/cancel an order.

## Research conclusions for the playbook

The current Wyckoff/SMC vocabulary is useful as a hypothesis language, but each term must
become measurable. The agent's first edge-finding contribution is not prediction; it is
consistent labeling and honest denominators:

- count every valid opportunity, taken or skipped;
- retain the chart and data as they existed at decision time;
- separate higher-timeframe thesis from lower-timeframe entry;
- version equal-high/low tolerance, swing sensitivity, displacement threshold, POI freshness,
  session, news proximity, and invalidation;
- measure MFE/MAE, realized R, planned versus actual risk, and management decisions;
- segment by volatility and directional/rotational regime;
- freeze the discovery sample before evaluating a rule.

For the described 1:6 XAUUSD short, the journal should distinguish the quality of the short
thesis from the decision to close three quarters at 4R, the runner logic, and the considered
countertrend hedge. “Could have closed for more” is hindsight unless an exit rule defined
the retracement behavior before the trade. That distinction is exactly what the new memory
and evaluation system should enforce.
