# Architecture

## Current milestone

The MVP is a human-in-the-loop journal and playbook service:

- FastAPI HTTP API and a minimal local interface.
- PostgreSQL locally through Docker or remotely through Neon.
- Deterministic position sizing.
- Structured trade plans and post-trade reflections.
- Provider-neutral chart analysis with strict observation/hypothesis separation.
- Provider-neutral chat and vision with OpenAI, Anthropic, and local Ollama adapters.
- Progressive task context from a compact, application-owned trading harness.
- A versioned runtime policy loaded at startup and checked by hooks before tool execution.
- Human-readable session names backed by internal UUIDs.
- Explicit workspace/account request scope for every decision, memory, evidence, and
  broker-ingestion operation.
- Account selection that keeps personal, prop, demo, scalp, intraday, and swing histories
  separate while allowing workspace-owned immutable strategies to be reused deliberately.
- Alembic-managed, execution-centered PostgreSQL records.
- Provider-neutral read-only broker and market-data contracts.
- Bounded in-memory quote/candle state with explicit freshness checks.
- A read-only OANDA implementation with transaction cursors and reconciliation.
- Typed broker sync pages with explicit cursor bounds, pagination, history coverage, and
  conflict-aware idempotency.
- Provider qualification that separates implemented, configured, reachable, and
  evidence-observed states, with optional bounded live read-only probes.
- Broker-contract-aware deterministic sizing and versioned instrument specifications.
- Trading Economics calendar/news metadata ingestion.
- Tiered local, connector, allowlisted-web, and optional broad-search retrieval.
- Host-owned response provenance independent of model-generated citation formatting.
- Provider usage aggregation and approximate model-cost reporting.
- Content-addressed chart evidence and auditable model-analysis runs.
- Immutable playbook definitions and sample-aware edge segmentation.
- Trader profiles and source-hashed Discord, Telegram, X, file, directory, and paste imports.
- Exact-version strategy knowledge isolation and scoped model query tools.
- Frozen backtest/forward-test samples, exclusions, reports, and numeric feature correlations.
- Startup economic-calendar refresh and trade-intent event context.

## Trust boundaries

1. Market prices, economic events, fills, and positions must come from timestamped tools.
2. The model may summarize evidence but cannot manufacture missing observations.
3. Risk calculations run in deterministic application code.
4. No order-placement tool is exposed.
5. Future broker work starts read-only, then adds order preview, then explicit confirmation.
6. Discord is an interface adapter only; it never owns domain logic or credentials.
7. Model-provider SDKs are adapters; journal, risk, policy, and evidence logic are independent.
8. `app/trading-rules.json` is hashed at startup. A mid-run policy change halts tool execution
   until restart.
9. Web pages and search snippets are untrusted data. They cannot modify policy, expand the
   tool surface, or directly invoke development or trading actions.
10. The host, not the language model, owns the final reference ledger. Selected prompt
    resources and executed data tools are recorded even when the model omits citations.
11. Imported material is untrusted and is never model instruction. Every knowledge query
    requires one exact playbook-version ID.
12. A historical successful sync is labeled as prior evidence, never as proof that a
    provider is reachable now. Live verification is explicit, read-only, and non-persistent.
13. The CLI/API edge resolves one immutable `(workspace_id, account_id)` scope before a
    service runs. Account-owned reads and writes fail closed when scope is absent or invalid.
14. Composite database foreign keys prevent an account-owned record from referencing a
    parent in another workspace/account. PostgreSQL row-level security is not enabled yet;
    hosted multi-user deployments still require identity/authentication and RLS or an
    equivalent database-enforced authorization layer.

## Adapter boundary

- Market data: OANDA first, MT5 reference; continuous data remains in memory.
- News/calendar: free Forex Factory weekly calendar events or Trading Economics metadata,
  with provider and retrieval timestamps.
- Web: read-only full-page retrieval for an explicit domain allowlist; redirects remain
  inside the allowlist and public network.
- Search: optional Brave Search discovery after local, connector, and allowlisted sources
  are insufficient. Results are snippets until a domain is deliberately allowlisted.
- Broker: read-only account, positions, and transaction/fill ingestion first.
- Discord: slash commands and image uploads calling this API.
- Storage: local private content-addressed files for chart evidence; PostgreSQL stores
  metadata and hashes under workspace/account-specific directories and rows. Object storage
  is a future deployment adapter.
- Knowledge: normalized imported text is stored in PostgreSQL under an immutable
  playbook-version foreign key; no unrestricted SQL tool is exposed to the model.

The database contains order-intent and approval records so a later preview workflow can be
audited. Those records do not grant execution authority. Connector protocols intentionally
have no place, modify, cancel, close, or hedge methods.

## Request and provenance flow

1. Deterministic harness routing selects local resources and records their path/hash.
2. The host resolves and validates one workspace/account scope; all subsequent account-owned
   context, broker reads, journal queries, and mutations use that same scope.
3. Recent conversation context, when present, is filtered to that account and exact strategy
   state, then content-hashed into the reference ledger.
4. Model routing chooses provider, model, and reasoning effort; the CLI computes an
   approximate first-response cost.
5. Read-only tools may add journal, evidence, broker, news, calendar, allowlisted-page, or
   search-result references. Mutating journal tools still pass policy and confirmation hooks.
6. Provider-reported usage is accumulated across every response and nested chart-analysis
   call.
7. The CLI renders the response and appends the host-owned reference ledger, provider-reported
   token usage, and estimated API cost.
