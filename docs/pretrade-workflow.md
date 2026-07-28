# Guided pre-trade workflow

`trade preflight` consolidates the safety-critical checks that previously required several
commands. The original `risk`, `instrument risk`, `plan`, `mindset`, `broker`, and `news`
commands remain available.

## Start

You can ask naturally:

```text
trade
I want to take a trade. Help me evaluate this setup.
```

The agent offers the guided preflight. If no exact strategy is active, it now:

1. Shows every saved strategy and lets you activate one.
2. Offers to build a new strategy if none fits.
3. Walks through methodology, objective, setup, observable context requirements, entry
   confirmations, stand-aside conditions, risk ceiling, minimum planned R, mindset caution
   tags, excluded cross-strategy concepts, and the evidence sample required before calling it
   an edge.
4. Shows the exact immutable definition for confirmation.
5. Activates that version and resumes the original preflight automatically.

You can still select a strategy manually with `/strategy use NAME` or start directly with
`trade preflight`; the direct command uses the same guided recovery when needed.

Use `--session NAME` when the latest session is not the intended one. A prepared
`TradePlanCreate` JSON can be supplied with `--file`. `--live-market` optionally reads an
OANDA quote and recent candles and computes deterministic features; it never places an
order.

## What is checked

The host application, not the language model:

1. Pins the exact active strategy version and definition hash.
2. Selects one setup key when a strategy defines multiple setups.
3. Shows every exact requirement and exclusion and asks the trader to mark it yes, no, or
   unknown.
4. Refreshes the configured calendar when possible, reports whether stored news evidence
   is fresh, stale, unavailable, or not configured, and displays nearby events.
5. Calculates risk, position size, and planned reward-to-risk with deterministic code.
6. Requires a thesis, invalidation, direct observations, and separately labeled hypotheses.
7. Records readiness, predefined-risk acceptance, emotion tags, and a process note.
8. Produces an adherence rating and asks for the final human decision.

The rating is one of:

- **Eligible:** all defined rules and evidence checks are satisfied.
- **Conditional:** a required confirmation or evidence source is missing.
- **Stand aside:** a defined exclusion applies or a required rule is not met.
- **Blocked:** a deterministic risk limit, minimum planned R, or explicit risk-acceptance
  rule fails.

Component scores describe rule and evidence completeness. They are not probabilities,
confidence estimates, forecasts, or individualized recommendations.

## Audit trail

Every completed workflow is saved to `pretrade_assessments`, including Blocked and Stand
aside outcomes for which no trade plan exists. The record contains:

- exact playbook version and definition hash through its immutable relation;
- exact setup key and per-rule statuses;
- component scores, blockers, stand-aside reasons, and missing evidence;
- news state and optional read-only market context;
- runtime policy hash;
- linked pre-trade mindset check-in;
- final human decision;
- a linked trade plan only when the trader chooses to proceed.

This requires database revision `d95b7a2e4f10`. Run `trade db upgrade` after updating.

## Execution boundary

The workflow never places, changes, cancels, hedges, or closes an order. Eligible means only
that the proposed plan meets the selected strategy's recorded rules. The trader retains the
final choice and must act separately at the broker.
