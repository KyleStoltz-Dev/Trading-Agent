# Custom strategies and trading rules

Trading Agent lets a trader define a personal decision process without turning the model’s
suggestions into hidden behavior. A strategy definition is an explicit, immutable checklist
used by preflight, journaling, backtests, and forward tests.

## Define rules in normal language

In interactive `trade` chat, describe the strategy and its boundaries:

```text
Create a strategy named gold-ny-reclaim.

Objective: trade only a declared New York sweep and reclaim.
Requirements:
- the 4-hour thesis and invalidation exist first;
- price sweeps a level marked before the event;
- a 5-minute candle closes back through that level.
Exclusions:
- high-impact news is within 15 minutes;
- the higher-timeframe thesis has invalidated.
Risk: maximum 0.5% and minimum 3R.
```

The agent may ask which rules apply to the entire strategy and which belong to one named
setup. It then validates and normalizes the proposal. Review the complete definition, risk
values, change hypothesis, minimum test sample, and proposal hash before confirming.

Validation does not write to PostgreSQL. Creation is a separate action and requires terminal
confirmation. A declined or stale proposal is not saved. The model cannot use confirmation
for one proposal to save different rules.

For an existing strategy, say:

```text
Add “Do not enter after the original thesis invalidates” as an exclusion to my
active strategy.
```

The active exact version is the only permitted base. The result is a new version under the
same strategy name. Earlier versions, journal entries, and experiments remain unchanged.
Creating the version does not automatically switch the current session; inspect its name,
version, and hash before selecting it.

## Supported definition

The runnable example is
[`docs/playbook-schema-v1.json`](playbook-schema-v1.json). The supported fields are:

- `methodology` and `objective`;
- strategy-wide `requirements` and `exclusions`;
- `context.required` and `context.exclusions`;
- named `setups`, each with its own requirements and exclusions;
- optional `composition` roles and an enforceable `conflict_rule`;
- `allowed_vocabulary` and `forbidden_cross_strategy_concepts`;
- mindset caution tags;
- maximum risk, minimum planned R, and mandatory human confirmation.

Unknown fields fail validation instead of being silently ignored. Rules and setup keys are
bounded, normalized, and must be unique. A definition must contain at least one enforceable
requirement, exclusion, conflict rule, or forbidden concept. A strategy may make risk stricter,
but cannot raise the application-wide maximum or disable human confirmation.

The offline JSON fallback uses the same validator:

```bash
trade strategy create \
  --name gold-ny-reclaim \
  --file /absolute/path/to/gold-ny-reclaim.json \
  --description "Personal New York sweep/reclaim process" \
  --hypothesis "Predeclared context plus reclaim improves process consistency" \
  --minimum-sample 30
```

## Isolation and evidence

One conversation uses one exact immutable strategy version. Pure Wyckoff and pure ICT/SMC
definitions are not blended automatically. To test a combination, create a third, explicitly
named combined strategy and state what each framework contributes plus what happens when
they conflict.

Imported messages, screenshots, broker observations, news, and web pages can support research,
but they are untrusted evidence. They cannot modify rules, select a strategy, weaken the risk
ceiling, authorize a tool, or change the read-only broker boundary.

Preflight presents each text rule as a yes/no/unknown question. The resulting assessment
measures completeness and self-reported adherence. It does not independently prove a visual
pattern, predict a trade outcome, or place an order. When a condition needs objective testing,
define its measurement separately and freeze that definition before collecting backtest or
forward-test samples.
