# Execution-centered data model

## Storage rule

PostgreSQL is the durable decision and execution ledger. It stores state that must be
reconciled, reviewed, or audited. It does **not** store the continuous quote stream.

For a live, readable inventory, run `trade data status`. To inspect all application tables
and columns, run `trade data schema`. The latter is a schema viewer, not an arbitrary SQL
console.

The live-data process keeps only bounded, replaceable state in memory:

- latest bid/ask with market and retrieval timestamps;
- recent candles in capped ring buffers;
- current account and position state for one explicit workspace/account scope;
- a freshness limit that fails closed before agent analysis.

Important live state is persisted only when a durable event occurs: a plan is frozen, a
fill arrives, management changes, reconciliation runs, or a review is saved.

## Ownership hierarchy

Every decision and memory operation runs under one immutable request scope:

```text
workspace
  -> trading account
     -> conversations, profile, constraints, plans, trades, mindset, evidence,
        broker ingestion, tests, alerts, and review records
```

The workspace owns reusable strategy definitions and their immutable versions. The account
owns the trader profile and every record whose meaning can change with capital, broker,
prop-firm rules, execution style, or account history. A strategy version may be used by
several accounts in the same workspace, but an account query never searches another
account's conversations, memories, evidence, plans, tests, or broker ledger.

Application services require a concrete `(workspace_id, account_id)` pair; they do not
silently fall back to a global/default account. Composite PostgreSQL foreign keys also reject
relationships that cross an account or workspace boundary. For example, a reflection cannot
attach to another account's plan, a conversation turn cannot attach to another account's
session, and a broker fill cannot attach to another account's connection or trade.

## Durable entities

| Concern | Tables | Why it is durable |
| --- | --- | --- |
| Ownership | `workspaces`, `trading_accounts` | Establishes the tenant and account boundary used by every decision, memory, evidence, and broker-ingestion query. |
| Identity | `instruments`, `instrument_mappings`, `instrument_specifications` | Maps symbols and versions the broker contract, costs, margin, and sizing increments used by each plan. |
| Account rules | `account_constraint_profiles` | Stores the trader-entered personal/prop classification, starting size, phase, and bounded challenge restrictions separately from credentials and broker truth. |
| Broker accounts | `trading_accounts`, `broker_connections` | Identifies the venue and account while storing only a secret-store reference, never credentials. |
| Sync | `connector_cursors` | Resumes transaction ingestion idempotently after restarts. |
| Playbook | `playbooks`, `playbook_versions` | Freezes the exact rules used by a plan so later edits cannot rewrite history. |
| Trader context | `trader_profiles`, `conversation_sessions`, `conversation_turns` | Stores style/preferences, the selected strategy, and the exact strategy version under which each turn was created. |
| Learning | `learning_curricula`, `learning_modules` | Stores experience-adjusted teaching mode, selected topics, ordered objectives, source plans, progress, notes, and cited evidence without changing execution strategies. |
| Strategy knowledge | `knowledge_imports`, `strategy_knowledge_items` | Hashes and deduplicates imported Discord, Telegram, X, and file content under one exact playbook version. |
| Strategy testing | `strategy_experiments`, `strategy_test_samples` | Freezes the rules hash and retains eligible, excluded, and unclear backtest/forward-test observations. |
| Decision | `trade_plans`, `market_contexts`, `observations`, `evidence_items`, `analysis_runs` | Separates facts from hypotheses and records content, policy, prompt, model, input, and output provenance. |
| Events | `economic_events`, `news_items` | Retains provider IDs, source/retrieval timestamps, importance, values, and links without copying full articles. |
| Chart alerts | `tradingview_alerts` | Retains verified, replay-safe alert facts and OHLCV without treating alert text as instructions or broker truth. |
| Lifecycle | `trades`, `trade_management_events` | Connects planning, broker records, fills, management decisions, and review for one position lifecycle. |
| Future preview | `order_intents`, `order_approvals` | Records a policy-bound proposal and the trader's separate decision. No connector exposes submission methods yet. |
| Execution | `execution_events`, `fills` | Stores normalized broker truth with external IDs, occurrence/ingestion times, and uniqueness constraints. |
| Snapshots | `position_snapshots`, `account_snapshots` | Captures state only at fills, management, review, manual capture, or reconciliation. |
| Review | `trade_reflections`, `rule_evaluations`, `mindset_checkins` | Keeps process quality, outcome, rule adherence, and mindset separately measurable. |

Screenshots and binary documents stay outside PostgreSQL. `evidence_items` stores their URI,
SHA-256 hash, MIME type, source, market time, retrieval time, and safe metadata. Normalized
imported message/note text is stored in PostgreSQL because it must be queryable, deduplicated,
and isolated by strategy version.

## Write and trust boundaries

1. Broker and market adapters normalize provider payloads into provider-neutral decimal
   and timezone-aware types.
2. Live quotes/candles enter `LiveMarketCache`; no tick or quote table exists.
3. Broker transaction ingestion uses `(connection_id, external_event_id)` and
   `(connection_id, external_fill_id)` uniqueness to make retries safe. One normalized fill
   may explicitly open, reduce, or close multiple external trade lifecycles; every effect is
   retained as sanitized execution metadata while the matching lifecycle rows receive their
   `open`, `partially_closed`, or `closed` state and close time.
4. Management events append the decision, size change, resulting position, realized R,
   reason, and time separately from the broker fill.
5. Provider metadata is sanitized. Full broker payloads and credentials are not retained.
   Provider-reported commission, financing, guaranteed-execution fee, and half-spread cost
   are normalized into dedicated nullable fill fields when supplied.
6. An order intent is not an order. It is an auditable proposal bound to an idempotency
   key, policy hash, expiry, and separate approval record.
7. The current connector protocols are read-only and contain no place, modify, cancel,
   close, or hedge method.
8. Every broker-sized plan references the exact effective instrument specification used
   for risk, cost, quantity, and margin arithmetic.
9. Chart evidence is content-addressed. Re-analyzing the same image creates a new
   `analysis_run` without duplicating the underlying evidence file or item.
10. Imported material is tied to `playbook_version_id`; searches always filter that exact
    value and excluded items. The model has no arbitrary SQL access.
11. Strategy experiments retain `rules_hash`. Eligibility changes require a new version and
    experiment rather than rewriting a running sample.
12. UUIDs remain relational primary keys. Sessions use unique names within an account, trade
    plans use generated references such as `xauusd-20260725-ny-short-1`, and experiments
    accept account-scoped names.
13. Prompt history is fail-closed: an active strategy sees only turns tagged with that exact
    immutable version; general mode sees only untagged turns. The transcript viewer remains a
    complete audit trail but is never used as unfiltered model context.
14. Curriculum modules are educational records. Framework education cannot modify an immutable
    playbook or become execution guidance unless that exact strategy version is active.
15. An account constraint profile is a reminder source, not live compliance evidence. Each
    pre-trade assessment retains the exact profile ID used, while current equity, daily P&L, and
    firm-side rule state must come from fresh broker/provider evidence before a compliance claim.
16. TradingView deliveries are assigned to the workspace/account named by the webhook route.
    They cannot select or mutate the active strategy, and their condition, note, and metadata
    values are untrusted model input.
17. A reconciliation account snapshot links to the latest transaction observed in that sync.
    An aggregate broker position links to a lifecycle trade only when exactly one active trade
    matches the account and instrument; ambiguous hedged/multi-trade positions remain unlinked
    instead of being guessed.
18. If transaction history begins after a trade opened, later reduce/close effects remain on the
    execution record but do not invent a missing opening lifecycle. Import earlier history or
    reconcile it explicitly.
19. `playbook_versions` is append-only at the PostgreSQL layer. Updates and deletes are rejected;
    revisions create a new row, and reads verify the stored definition against its SHA-256 hash.
20. Strategy names are unique case-insensitively, preventing ambiguous isolation between names
    such as `Wyckoff` and `wyckoff`.
21. Mindset check-ins store both normalized `emotion_tags` for grouping and the trader's exact
    `emotional_state` wording for honest reflection. Profanity is preserved. Reflective text is
    bounded, rendered literally, treated as untrusted data when shown to a model, and never used
    by itself as an execution signal or diagnosis.
22. Instrument specifications may be global reference contracts or scoped to one account.
    Account-specific contracts take precedence; their workspace/account fields must either
    both be present or both be absent.

## Initial providers

- OANDA v20: normalize price/candle data into the live cache and ingest account
  transactions into execution records.
- MetaTrader 4/5: normalize fixed read-only bridge responses into the same cache, execution
  ledger, account snapshots, and net position snapshots as OANDA. The included MT5 companion
  runs beside an official Windows terminal. Ambiguous deal history retains the external
  position ID but cannot invent an open/reduce/close lifecycle relationship.

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

The account-isolation migration `a73f1c9d4e20` expands the existing schema, creates the
deterministic `legacy-local` workspace, preserves known account relationships, assigns
truly unscoped legacy rows to an inactive `Legacy / unassigned` account, validates every
relationship, and only then makes scope columns and composite foreign keys mandatory. It
does not retain server defaults for scope, so new writes must identify their workspace and
account explicitly.

Back up before applying this boundary. Its downgrade intentionally stops instead of
automatically collapsing account-specific profiles, decisions, and evidence into one account;
that collapse would be lossy.

An unmanaged pre-Alembic schema is never stamped or overwritten automatically. See
`docs/operations.md` for the backup, transactional adoption, and row-count verification
workflow.
