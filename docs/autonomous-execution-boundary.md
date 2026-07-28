# Autonomous execution boundary

## Current status

This document defines a possible future architecture. It does not authorize or implement
broker execution.

Trading Agent is currently decision support. Its broker protocols are read-only, its runtime
policy forbids order tools, and the conversational process must never place, modify, cancel,
close, or hedge an order. Existing `order_intents` and `order_approvals` tables are audit and
preview records, not an execution path.

The boundary was first researched on 2026-07-23 and aligned with the current runtime policy
on 2026-07-26. It must be re-reviewed against then-current broker behavior, law, security
practice, and product policy before any execution work begins.

## Why execution is separate

An analytical assistant can give poor advice. An execution system can turn the same error
into immediate, repeated, leveraged loss. Broker credentials, order mutation, fresh account
truth, reconciliation, and capital protections therefore belong in a separate deployable
service with narrower dependencies and independent controls.

“Autonomous subagent” describes who proposes an action. It is not a safety boundary. Safety
comes from deterministic authorization and execution code that the model cannot alter or
bypass.

## Target architecture

```text
market/news evidence ──> frozen strategy or model ──> TradeIntent
                                                     │
broker/account truth ─────────────────────────────────┤
approved policy and limits ───────────────────────────┤
                                                     ▼
                                           deterministic RiskGate
                                              │             │
                                           reject        authorize
                                                               │
                                                               ▼
                                                   ExecutionEngine
                                                               │
                                                               ▼
                                                            Broker
                                                               │
                                                               ▼
                                               reconciliation and audit
```

The model never receives raw `place_order`, `modify_order`, or `close_position` tools. It
may propose a typed intent. The execution engine accepts only a short-lived authorization
created from current broker truth and an approved, immutable policy.

The current conversational application, future risk gate, and future execution worker must
be separately deployable and separately permissioned.

## Records

### TradeIntent

A future intent should contain:

- intent ID and idempotency key;
- exact strategy and playbook version;
- instrument, venue, direction, and order preference;
- proposed entry, stop, target, and expiry;
- evidence snapshot IDs and market timestamp;
- thesis, invalidation, and data-quality flags;
- requested risk, never authoritative final quantity.

The current preview schema includes quantity. Before building an execution plane, revise or
strictly reinterpret that field so a model-provided quantity can never bypass deterministic
sizing.

### RiskDecision

- accepted or rejected;
- policy version and every evaluated rule;
- account, position, quote, and instrument-specification snapshot IDs;
- calculated maximum quantity and worst-case loss;
- reason codes and authorization expiry;
- cryptographic binding to the complete original intent.

### Order lifecycle

At minimum:

```text
CREATED → SUBMITTING → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED
```

Terminal and recovery states include:

```text
REJECTED  CANCELED  EXPIRED  UNKNOWN  RECONCILING  HALTED
```

Every transition is append-only. Current state is a projection, not the only record.

## Deterministic hard limits

These controls cannot exist only as prompt instructions:

- exact approved account, broker, venue, and symbol allowlists;
- maximum risk per trade and maximum aggregate open risk;
- daily and weekly realized plus mark-to-market loss limits;
- maximum quantity, notional, leverage, and concurrent positions;
- maximum submissions and amendments in a time window;
- maximum quote age, spread, slippage, and clock skew;
- allowed sessions and rollover blackout;
- scheduled high-impact-event blackout;
- required protective stop and verified broker semantics;
- duplicate and idempotency rejection;
- healthy reconciliation;
- operator state and independent kill switch.

Any missing, stale, contradictory, or unknown input rejects the intent.

The model cannot increase risk because confidence is high, retry a rejection by itself, widen
a stop, or treat an unavailable control as optional.

## Credentials and process isolation

- The conversational process has no execution credential.
- The execution service has no general shell, arbitrary code execution, broad MCP surface,
  model-development tool, or web browser.
- A dedicated database role can append execution events but cannot rewrite research history.
- The risk gate and execution worker use separate least-privilege identities where practical.
- The kill switch runs outside the model and execution worker.
- Secrets never enter prompts, traces, resolved configuration, or error payloads.
- Network egress is restricted to required broker, data, monitoring, and time sources.
- Policy and adapter releases require signed artifacts and controlled deployment.

## Human approval

Approval is necessary during paper and supervised phases, but a confirmation button alone is
not sufficient.

A valid approval must be:

- bound to the exact intent hash, strategy version, policy version, account, and calculated
  quantity;
- short-lived and one-time;
- rejected after any price, spread, account, risk, policy, or intent change beyond defined
  tolerances;
- followed by a fresh deterministic risk check immediately before submission.

The current HTTP confirmation challenge protects API mutations from replay and body
substitution. It is not an execution authorization and is not a second identity factor.

## Required failure tests

Before any live phase, test:

- repeated submission of one intent;
- timeout after broker acceptance but before local acknowledgement;
- partial fill followed by disconnect;
- duplicated and out-of-order broker events;
- stale quotes, spread spikes, and clock skew;
- stale equity or an unknown open position;
- database unavailability before and after submission;
- restart during every order state;
- a risk-limit change while authorization is pending;
- kill switch before submission and during an open position;
- broker rejection, market closure, margin change, and symbol remapping;
- model, prompt, provider, or policy changes during an evaluation window.

The safe response is not always “cancel everything.” Blind cancellation or hedging during
unknown state can increase exposure. Halt new actions, reconcile broker truth, then follow a
predefined recovery policy.

## Promotion gates

### Gate 0 — research only

- No order adapter is installed.
- Strategy definitions and evidence schemas are stable enough to replay.
- Data provenance and basic look-ahead checks pass.

This is the current product boundary.

### Gate 1 — historical replay

- A frozen strategy passes untouched out-of-sample and regime tests.
- Costs and adverse fills are modeled.
- Results include uncertainty and multiple-testing adjustment.
- A simple baseline does not explain the claimed advantage.

### Gate 2 — live shadow

- Intents run on timestamped live data with no submission capability.
- Staleness, downtime, and decision latency are measured.
- Hypothetical fills are reconciled against observable market data.
- The evaluation window is not used to tune prompts or rules.

### Gate 3 — paper execution

- The real order state machine runs against a simulator or broker paper account.
- Fault injection, idempotency, recovery, reconciliation, and kill-switch tests pass.
- Operator alerts have documented response procedures.
- Human approval is still required.

### Gate 4 — supervised micro-live

- Separate credentials and least privilege are verified.
- Every order requires expiring, intent-bound human approval.
- Risk is the smallest broker-supported amount.
- Independent reconciliation shows no unexplained state.

### Gate 5 — bounded autonomy

- Only a frozen, approved strategy can propose submission.
- Symbol, session, order type, size, and loss limits are narrowly allowlisted.
- Independent monitoring can halt the service.
- Any model, prompt, policy, data-provider, or broker-adapter change returns the system to a
  prior gate.

Promotion depends on evidence, not elapsed time, star counts, model confidence, or a strong
backtest.

## Appropriate model role

A model may:

- classify evidence against a versioned checklist;
- identify missing or contradictory facts;
- propose an intent with explicit invalidation;
- summarize news with publication and event timestamps;
- explain a deterministic rejection;
- produce post-trade reflection and candidate research hypotheses.

A model may not:

- calculate authoritative size or margin;
- bypass a failed gate;
- infer a current price from a screenshot;
- decide stale data is probably acceptable;
- retry a rejected order without new authorization;
- widen a stop or increase risk because of conviction;
- change live policy or promote its own strategy.

## First safe experiment

The first experiment is not “let the agent trade.” Run the approved context and trigger
checklist in shadow mode during the trader's normal XAUUSD sessions. Compare:

- agent and trader higher-timeframe premises;
- operational trigger classifications;
- taken, skipped, rejected, and missed opportunities;
- proposed versus actual invalidation;
- simulated execution under conservative costs;
- differences by regime, session, and timeframe pair.

This tests whether the agent improves consistency before asking whether it should control
execution.
