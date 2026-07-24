# Execution-centered data model

## Storage rule

PostgreSQL is the durable decision and execution ledger. It stores state that must be
reconciled, reviewed, or audited. It does **not** store the continuous quote stream.

The live-data process keeps only bounded, replaceable state in memory:

- latest bid/ask with market and retrieval timestamps;
- recent candles in capped ring buffers;
- current account and position state;
- a freshness limit that fails closed before agent analysis.

Important live state is persisted only when a durable event occurs: a plan is frozen, a
fill arrives, management changes, reconciliation runs, or a review is saved.

## Durable entities

| Concern | Tables | Why it is durable |
| --- | --- | --- |
| Identity | `instruments`, `instrument_mappings`, `instrument_specifications` | Maps symbols and versions the broker contract, costs, margin, and sizing increments used by each plan. |
| Accounts | `trading_accounts`, `broker_connections` | Identifies the venue and account while storing only a secret-store reference, never credentials. |
| Sync | `connector_cursors` | Resumes transaction ingestion idempotently after restarts. |
| Playbook | `playbooks`, `playbook_versions` | Freezes the exact rules used by a plan so later edits cannot rewrite history. |
| Decision | `trade_plans`, `market_contexts`, `observations`, `evidence_items`, `analysis_runs` | Separates facts from hypotheses and records content, policy, prompt, model, input, and output provenance. |
| Events | `economic_events`, `news_items` | Retains provider IDs, source/retrieval timestamps, importance, values, and links without copying full articles. |
| Lifecycle | `trades`, `trade_management_events` | Connects planning, broker records, fills, management decisions, and review for one position lifecycle. |
| Future preview | `order_intents`, `order_approvals` | Records a policy-bound proposal and the trader's separate decision. No connector exposes submission methods yet. |
| Execution | `execution_events`, `fills` | Stores normalized broker truth with external IDs, occurrence/ingestion times, and uniqueness constraints. |
| Snapshots | `position_snapshots`, `account_snapshots` | Captures state only at fills, management, review, manual capture, or reconciliation. |
| Review | `trade_reflections`, `rule_evaluations`, `mindset_checkins` | Keeps process quality, outcome, rule adherence, and mindset separately measurable. |

Screenshots and documents stay outside PostgreSQL. `evidence_items` stores their URI,
SHA-256 hash, MIME type, source, market time, retrieval time, and safe metadata.

## Write and trust boundaries

1. Broker and market adapters normalize provider payloads into provider-neutral decimal
   and timezone-aware types.
2. Live quotes/candles enter `LiveMarketCache`; no tick or quote table exists.
3. Broker transaction ingestion uses `(connection_id, external_event_id)` and
   `(connection_id, external_fill_id)` uniqueness to make retries safe.
4. Management events append the decision, size change, resulting position, realized R,
   reason, and time separately from the broker fill.
5. Provider metadata is sanitized. Full broker payloads and credentials are not retained.
6. An order intent is not an order. It is an auditable proposal bound to an idempotency
   key, policy hash, expiry, and separate approval record.
7. The current connector protocols are read-only and contain no place, modify, cancel,
   close, or hedge method.
8. Every broker-sized plan references the exact effective instrument specification used
   for risk, cost, quantity, and margin arithmetic.
9. Chart evidence is content-addressed. Re-analyzing the same image creates a new
   `analysis_run` without duplicating the underlying evidence file or item.

## Initial providers

- OANDA v20: normalize price/candle data into the live cache and ingest account
  transactions into execution records.
- MetaTrader 5: normalize terminal ticks and rates as a reference adapter. A dependable
  deployment will need a reachable MT5 terminal or a separately reviewed EA bridge.

Both providers map through `instrument_mappings`; neither is embedded in journal services
or model-provider code.

## Migration workflow

The CLI and API apply forward migrations at startup when
`DATABASE_AUTO_MIGRATE=true` (the local-development default). Manual fallback commands are:

```bash
trading-agent db status
trading-agent db upgrade
```

Production deployments can set `DATABASE_AUTO_MIGRATE=false` and run the upgrade command
as an explicit release step.

An unmanaged pre-Alembic schema is never stamped or overwritten automatically. See
`docs/operations.md` for the backup, transactional adoption, and row-count verification
workflow.
