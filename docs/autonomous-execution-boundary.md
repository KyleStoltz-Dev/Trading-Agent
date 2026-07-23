# Autonomous execution boundary

## Why it is separate

An analytical assistant can fail by giving poor advice. An execution system can turn the
same error into an immediate, repeated, leveraged loss. Broker credentials, order mutation,
real-time reconciliation, and capital protections therefore belong in a separate deployable
service with a narrower dependency set and independent controls.

“Autonomous subagent” describes who proposes an action. It does not provide the safety
boundary. The boundary comes from deterministic authorization and execution code that the
agent cannot alter or bypass.

## Target architecture

```text
market/news evidence ──> strategy or model ──> TradeIntent
                                                │
broker/account truth ────────────────────────────┤
approved policy + limits ────────────────────────┤
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
                                          reconciliation + audit
```

The model never receives a raw `place_order` capability. It submits a typed intent to the
risk gate. The execution engine accepts only a signed authorization produced from current
broker truth and an approved, versioned policy.

## Core records

### TradeIntent

- intent ID and idempotency key;
- strategy and playbook version;
- instrument, venue, direction, and order preference;
- proposed entry/stop/target and expiry;
- evidence snapshot IDs and market timestamp;
- thesis, invalidation, and data-quality flags;
- requested risk, never final quantity.

### RiskDecision

- accepted or rejected;
- policy version and every evaluated rule;
- account/position/quote snapshot IDs;
- calculated maximum quantity and worst-case loss;
- reason codes and authorization expiry;
- cryptographic link to the original intent.

### Order lifecycle

At minimum:

`CREATED → SUBMITTING → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED`

with terminal or recovery states:

`REJECTED`, `CANCELED`, `EXPIRED`, `UNKNOWN`, `RECONCILING`, `HALTED`.

Every transition is append-only. Current state is a projection, not the only record.

## Deterministic hard limits

These checks cannot be prompt instructions:

- approved account, broker, venue, and XAUUSD symbol only;
- maximum risk per trade and maximum aggregate open risk;
- daily/weekly realized and mark-to-market loss limits;
- maximum order quantity, notional, leverage, and concurrent positions;
- maximum submissions/amendments in a time window;
- maximum quote age, spread, slippage tolerance, and data-clock skew;
- allowed sessions and rollover blackout;
- scheduled high-impact-news blackout;
- required protective stop and broker-supported order semantics;
- duplicate/idempotency rejection;
- reconciliation must be healthy;
- kill switch and operator state must permit submission.

Any missing, stale, contradictory, or unknown input rejects the intent.

## Credential and control separation

- The conversational copilot has no broker secret.
- The execution service has no general shell, arbitrary code execution, or broad MCP tools.
- A dedicated database role can append execution events but cannot modify research history.
- The kill switch runs outside the model and execution worker.
- Secrets never enter prompts, traces, resolved config, or error payloads.
- Network egress is limited to required broker, market-data, and monitoring endpoints.

## Required failure tests

Before any live phase, automate:

- repeated submission of the same intent;
- timeout after broker acceptance but before local acknowledgement;
- partial fill followed by disconnect;
- out-of-order and duplicated broker events;
- stale quote and spread spike;
- stale account equity or unknown open position;
- database unavailable before and after submission;
- worker restart during every order state;
- risk-limit change while an intent is pending;
- kill switch before submission and during an open position;
- broker rejection, market closure, margin change, and symbol remapping.

The safe response is not always “cancel everything.” During uncertain state, a blind cancel
or hedge can compound exposure. Halt new actions, reconcile broker truth, then follow a
predefined recovery policy.

## Promotion gates

### Gate 0 — research only

- No order adapter is installed.
- Strategy definitions and evidence schemas are stable enough to replay.
- Basic look-ahead and data-provenance checks pass.

### Gate 1 — historical replay

- Frozen strategy passes untouched out-of-sample and regime tests.
- Costs and adverse fills are modeled.
- Results include uncertainty and multiple-testing adjustment.
- A simple baseline does not explain the claimed advantage.

### Gate 2 — live shadow

- Intents run on live timestamped data with no submission capability.
- Staleness, downtime, and decision latency are measured.
- Hypothetical fills are reconciled against observable market data.
- No prompt or configuration changes are made using the evaluation window.

### Gate 3 — paper execution

- The real execution state machine runs against a simulator/paper account.
- Fault-injection, idempotency, recovery, reconciliation, and kill-switch tests pass.
- Operator alerts have documented response procedures.

### Gate 4 — supervised micro-live

- Separate credentials and least-privilege controls are verified.
- Every order requires an expiring human approval.
- Risk is limited to the smallest broker-supported amount.
- Independent reconciliation and daily review show no unexplained state.

### Gate 5 — bounded autonomy

- Only a frozen, approved strategy can submit.
- Symbol, session, order types, size, and loss limits are narrowly allowlisted.
- Independent monitoring can halt the service.
- Any policy, model, prompt, data-provider, or broker-adapter change returns the system to a
  prior gate.

## Model role

Appropriate model tasks:

- classify evidence against a versioned checklist;
- identify missing or contradictory facts;
- propose an intent with explicit invalidation;
- summarize relevant news with publication/event timestamps;
- explain a rejection;
- produce post-trade reflection and candidate research hypotheses.

Inappropriate model tasks:

- calculate authoritative size or margin;
- bypass a failed gate;
- infer a current price from a screenshot;
- choose whether stale data is “probably fine”;
- retry rejected orders without a new authorization;
- widen a stop or increase risk because of conviction;
- modify live policy or promote its own strategy.

## First autonomous experiment

The first experiment should not be “let the agent trade.” It should run the approved
context/trigger checklist in shadow mode during Kyle's normal XAUUSD sessions. Compare:

- whether the agent and trader identified the same higher-timeframe premise;
- whether each trigger met the same operational definition;
- taken, skipped, and rejected opportunities;
- proposed versus actual invalidation;
- simulated execution under conservative costs;
- differences by regime and timeframe pair.

This directly tests whether the agent improves consistency before testing whether it should
control execution.

