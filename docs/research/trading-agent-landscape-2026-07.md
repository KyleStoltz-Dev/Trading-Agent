# Trading-agent landscape review — July 2026

## Status and caveat

This is a dated architecture review, not a live popularity ranking. The repository and
popularity snapshot was inspected on 2026-07-23. Star counts, project structure, licenses,
features, and links can change after that date. Stars indicate adoption and maintenance
interest; they do not demonstrate safety, accuracy, profitability, or suitability for live
capital.

The patterns below are design references only. No third-party implementation code was copied.
GPL- or AGPL-licensed code must not be incorporated without a deliberate licensing decision.

## Decision summary

Trading Agent should not clone one project. The strongest combination is:

- **Vibe-Trading:** one domain core reused by interactive, API, and future MCP surfaces;
- **TradeMemory:** recall before decisions, record no-trades as well as trades, and reflect
  after outcomes;
- **TradingAgents:** typed, bounded workflow stages and resumable checkpoints;
- **ai-hedge-fund:** deterministic allowed actions and risk limits before model reasoning;
- **OpenBB:** provider-neutral data contracts with explicit provenance;
- **Freqtrade:** explicit operating modes, configuration validation, protections, and
  look-ahead diagnostics;
- **Hummingbot:** a separately deployed execution plane with order lifecycle,
  reconciliation, and restart recovery.

For the current product, the model remains a discretionary decision-support copilot. It may
structure a thesis, identify missing evidence, calculate risk through deterministic code,
record a confirmed plan, analyze a screenshot, and measure a frozen strategy sample. It
cannot place, modify, cancel, close, or hedge a broker order.

Any future autonomous system belongs in a separate service. A model may eventually propose a
typed intent, but deterministic authorization, final sizing, submission, reconciliation, and
the kill switch must remain outside the model.

## Snapshot

| Project | Stars observed 2026-07-23 | License observed | Strongest lesson | Fit |
| --- | ---: | --- | --- | --- |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 94.3k | Apache-2.0 | Typed, staged reasoning graph | Medium |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 70.9k | AGPL-3.0 | Provider-neutral data contracts | Medium |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 62.4k | MIT | Deterministic action envelope | Medium |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 52.6k | GPL-3.0 | Mature bot operations and bias checks | High for future execution |
| [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | 26.9k | MIT | CLI/API/MCP copilot experience | High |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 19.2k | Apache-2.0 | Order lifecycle and executor separation | High for future execution |
| [TradeMemory](https://github.com/mnemox-ai/tradememory-protocol) | 1.4k | MIT | Decision memory and reflection | High |

## Evaluation criteria

Each project was compared against the same needs:

1. Evidence integrity: source, venue, market time, retrieval time, and as-of behavior.
2. Decision quality: context, trigger, invalidation, alternatives, and uncertainty.
3. Memory: plans, skipped trades, fills, management, outcomes, and reflection.
4. Evaluation: replay, walk-forward testing, regime segmentation, and bias controls.
5. Risk containment: deterministic sizing, maximum loss, and fail-closed behavior.
6. Execution safety: idempotency, order states, reconciliation, recovery, and kill switch.
7. Interaction: practical CLI, images, reusable API/MCP capabilities, and inspectable traces.
8. Fit: discretionary intraday trading, especially XAUUSD, rather than equity-only research
   or crypto market making.

## TradingAgents

### What it is

TradingAgents uses a LangGraph workflow for equity research. Market, sentiment, news, and
fundamental analysts feed a bull/bear debate. A research manager, trader, risk roles, and
portfolio manager advance a bounded graph. Relevant references include its
[graph setup](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/graph/setup.py),
[conditional routing](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/graph/conditional_logic.py),
and [CLI package](https://github.com/TauricResearch/TradingAgents/tree/main/cli).

### Take

- Pass a typed state object through named stages.
- Bound transitions and debate rounds.
- Checkpoint a plan so it can resume during the session and after the outcome.
- Validate structured output and fail conservatively.
- Verify numeric market facts deterministically before the model reasons about them.
- Search explicitly for disconfirming evidence.

### Adapt

Replace equity personas with stages that match the discretionary workflow:

```text
context evidence
  → competing scenarios
  → trigger evidence
  → deterministic risk
  → management plan
  → journal
  → outcome reflection
```

Higher-timeframe context and lower-timeframe execution must remain separate. Dropping from
4h/1h to 15m/5m/1m cannot silently create a new thesis after the original one invalidates.

### Reject or defer

- Persona or famous-investor theater.
- Counting multiple model roles as independent confirmation.
- A coarse Buy/Hold/Sell label as the main output.
- Multi-agent debate on every request.
- Markdown files as canonical trading memory; PostgreSQL remains the system of record.

## OpenBB

### What it is

OpenBB is a financial-data platform rather than a trade-decision agent. Provider extensions
transform a common query, extract vendor data, and normalize it into standard models. Useful
references are its
[Fetcher interface](https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/abstract/fetcher.py),
[provider registry](https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/registry.py),
and [query executor](https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/query_executor.py).

### Take

- Small provider protocols with typed requests and results.
- Canonical schemas independent of the vendor.
- Capability discovery and explicit required credentials.
- Provenance on every observation.
- Contract tests shared by all adapters.

### Adapt

Keep the first contracts narrow:

- XAUUSD OHLC and bid/ask observations;
- high-impact USD and gold-relevant calendar events;
- timestamped headlines with source links;
- read-only account, position, transaction, and fill truth.

Fallbacks must be visible. Two providers with different symbols, sessions, venues, or
timestamps must not be silently presented as one equivalent series.

### Reject or defer

- Installing a broad data platform before narrow contracts are stable.
- Copying AGPL implementation code.
- Treating normalized fields as semantically identical without validating venue and time.

## ai-hedge-fund

### What it is

This educational equity project uses investor personas plus fundamental, technical,
sentiment, valuation, risk, and portfolio roles. The useful boundary is its
[risk manager](https://github.com/virattt/ai-hedge-fund/blob/main/src/agents/risk_manager.py),
[portfolio manager](https://github.com/virattt/ai-hedge-fund/blob/main/src/agents/portfolio_manager.py),
and V2 [broker protocol](https://github.com/virattt/ai-hedge-fund/blob/main/v2/brokers/protocol.py).

The risk layer constrains the available actions and quantities. A failure defaults to HOLD.

### Take

- Calculate the allowed action set before model reasoning.
- Keep risk math outside prompts.
- Default to no action when data, parsing, or dependencies fail.
- Distinguish intent, submitted order, fill, and position.
- Define broker semantics against simulation before any live adapter.

### Adapt

The current copilot's envelope is advisory:

```text
allowed: [NO_TRADE, PLAN_LONG, PLAN_SHORT]
maximum_risk: deterministic result
required_invalidation: level plus reason
```

A future model-proposed intent must still pass a deterministic risk gate using fresh broker
truth immediately before any submission.

### Reject or defer

- Investor personas as market evidence.
- Model confidence as a sizing input.
- A simplistic fill simulator as justification for live capital.
- Equity fundamentals as the center of an intraday XAUUSD workflow.

## Freqtrade

### What it is

Freqtrade is a mature autonomous crypto bot with dry-run/live modes, backtesting,
configuration validation, persistence, protections, exchange adapters, and operational
control. Relevant sources include
[configuration validation](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/configuration/config_validation.py),
[bot startup](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/freqtradebot.py),
and [look-ahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/).

### Take

- Explicit research, paper, and live modes; default to research or stopped.
- Validate cross-field invariants before startup.
- Display fully resolved, redacted configuration.
- Add symbol/session allowlists, cooldowns, circuit breakers, and a global kill switch.
- Reconcile persisted state against broker truth on restart.
- Test for look-ahead and recursive-indicator bias.
- Expose operator states such as STOPPED, RUNNING, PAUSED, DEGRADED, and HALTED.

### Adapt

Apply the operational rigor—not the crypto machinery—to XAUUSD venue hours, rollover,
spreads, slippage, margin, symbol mapping, macro-event blackouts, and stale data.

### Reject or defer

- Crypto exchange and pair-selection machinery.
- Auto-optimization as proof of an edge.
- Assuming a backtest configuration transfers unchanged to live execution.
- Copying GPL implementation code.

## Vibe-Trading

### What it is

Vibe-Trading is the closest interaction benchmark: FastAPI/React, an interactive CLI/TUI,
MCP, a journal, market-data fallbacks, backtests, and agent workflows. Its
[agent skill](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/SKILL.md) describes a
journal-to-rule shadow loop, while its
[MCP server](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/mcp_server.py) keeps
trading-connector tools read-only.

### Take

- One domain core shared by CLI, HTTP API, and MCP.
- Natural-language routes into journal, chart, review, and replay workflows.
- Persistent, resumable sessions.
- Read-only versus mutating capability metadata.
- Tool traces, timeouts, heartbeats, redaction, and usage records.
- Reproducible run cards with strategy/configuration/data hashes and warnings.
- Walk-forward, bootstrap, and Monte Carlo evaluation.

### Adapt

The existing Trading Agent already shares services between CLI and API and exposes a bounded
tool registry. The next useful adaptation is a typed, resumable trade-decision workflow and,
later, an MCP adapter that reuses those services and confirmation hooks.

Journal-derived rules remain candidate hypotheses. They must retain their discovery sample
and be evaluated on later untouched data by regime.

### Reject or defer

- Dozens of swarms before the core workflow is measurable.
- Silent provider fallback.
- Executing arbitrary model-generated backtest code.
- Treating feature breadth as production validation.

## Hummingbot

### What it is

Hummingbot is a mature automated crypto execution framework. Strategy V2 separates
controllers, which propose actions, from executors that own the order lifecycle. See its
[controller base](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/controllers/controller_base.py),
[executor orchestrator](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/executors/executor_orchestrator.py),
and [budget checker](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/connector/budget_checker.py).

### Take

- `TradeIntent → OrderCandidate → RiskDecision → BrokerOrder → Fill → Position`.
- Stable client-order IDs and idempotent submission.
- Reserve budget across concurrent candidates.
- Persist and reconcile every order transition.
- Separate controller logic from executor mechanics.
- Use a clock abstraction for replay.
- Recover after restart and handle unknown broker state explicitly.
- Keep the kill switch independent of the model.

### Adapt

Use these patterns only in a future, separately deployed execution service. The current
conversational process must not receive broker mutation methods or broker credentials.

### Reject or defer

- Market-making, DEX, wallet, and crypto-connector complexity.
- Assuming paper fills reproduce XAUUSD spread, rollover, rejection, or slippage.
- Sharing execution credentials with the conversational process.

## TradeMemory Protocol

### What it is

TradeMemory is a local MCP/REST memory and governance layer. It records decisions, recalls
prior evidence, reflects on outcomes, and maintains hashed audit records. It explicitly does
not execute. See its
[architecture](https://github.com/mnemox-ai/tradememory-protocol/blob/master/docs/ARCHITECTURE.md),
[MCP server](https://github.com/mnemox-ai/tradememory-protocol/blob/master/src/tradememory/mcp_server.py),
and [limitations](https://github.com/mnemox-ai/tradememory-protocol/blob/master/LIMITATIONS.md).

Its most important lesson is recording decisions not to trade, not only fills. It also
publishes negative results when generated strategies fail statistical promotion gates.

### Take

- Recall similar evidence before the next decision.
- Record no-trades, rejected plans, overrides, and management changes.
- Separate observation, decision, outcome, and reflection timestamps.
- Store the exact strategy version with every decision.
- Promote examples to beliefs only after sample-size and drift checks.
- Treat behavioral warnings as review prompts, not psychological diagnoses.
- Use out-of-sample and multiple-testing-aware gates for claimed edges.

### Adapt

Use PostgreSQL structured rows plus optional retrieval indexes. Semantic similarity must
never erase exact filters such as instrument, session, timeframe, strategy version, or as-of
date.

Keep these meanings separate:

- `trader_conviction`: self-report; never increases risk;
- `evidence_completeness`: deterministic checklist coverage;
- `edge_estimate`: statistical estimate with sample size and uncertainty;
- `data_quality`: freshness and source integrity.

### Reject or defer

- A hand-authored confidence score as a risk multiplier.
- Automatic belief induction from a selected journal.
- Calling an audit trail immutable when one administrator controls both rows and hashes.
- Parallel canonical database implementations.

## Pattern crosswalk

| Need | Inspiration | Current or intended Trading Agent adaptation |
| --- | --- | --- |
| Interactive use | Vibe-Trading | Typer/Rich CLI and API over shared services; MCP remains future |
| Decision workflow | TradingAgents | Guided preflight plus exact account/strategy/setup recall; resumable lifecycle remains future |
| Journal/memory | TradeMemory | Plans, no-trades, mindset, outcomes, and bounded comparable-decision recall |
| Data | OpenBB | Narrow provider contracts, provenance, and separate implementation/configuration/live qualification |
| Risk envelope | ai-hedge-fund | Deterministic risk tools and fail-closed preflight |
| Evaluation | TradeMemory, Vibe-Trading, Freqtrade | Frozen manual experiments exist; advanced replay statistics remain future |
| Operations | Freqtrade | Health/config validation exists; execution run states remain future |
| Execution | Hummingbot | Separate future service; no execution method in current connectors |

Do not combine persona count, broad dependencies, generated backtest code, and live broker
access in one process. That increases attack surface and destroys attribution: it becomes
unclear whether an action came from evidence, a prompt, a fallback provider, or execution
state.

## Product boundary and playbook conclusions

The current agent should help answer:

- What is directly visible, and what remains a hypothesis?
- What higher-timeframe condition makes the lower-timeframe setup relevant?
- What falsifies the thesis?
- Does the proposal meet the immutable checklist?
- What quantity corresponds to predefined risk under the active broker specification?
- What similar, reviewed samples exist under this exact strategy and regime?
- Was the result driven by setup, execution, management, or ordinary outcome variance?

It must not fabricate certainty, infer a current price from an old screenshot, rewrite rules
after seeing an outcome, or execute an order.

Wyckoff and ICT/SMC terminology is useful as hypothesis language only after operational
definition. Edge research should:

- count valid opportunities whether taken, skipped, or rejected;
- retain evidence as it existed at decision time;
- separate higher-timeframe context from lower-timeframe trigger;
- version equal-level tolerance, swing sensitivity, displacement threshold, imbalance,
  point-of-interest freshness, session, news proximity, and invalidation;
- measure MFE/MAE, planned and realized R, costs, and management decisions;
- segment by volatility, directional/rotational regime, session, and timeframe pair;
- freeze the discovery sample before evaluation.

For the documented XAUUSD short example, short-thesis quality, taking three quarters at 4R,
leaving a runner, and considering a countertrend hedge are four different decisions.
“Could have closed for more” is hindsight unless a retracement exit rule existed before the
trade. The journal and experiment design should preserve that distinction.
