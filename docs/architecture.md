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
- Alembic-managed, execution-centered PostgreSQL records.
- Provider-neutral read-only broker and market-data contracts.
- Bounded in-memory quote/candle state with explicit freshness checks.
- A read-only OANDA implementation with transaction cursors and reconciliation.
- Broker-contract-aware deterministic sizing and versioned instrument specifications.
- Trading Economics calendar/news metadata ingestion.
- Content-addressed chart evidence and auditable model-analysis runs.
- Immutable playbook definitions and sample-aware edge segmentation.

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

## Adapter boundary

- Market data: OANDA first, MT5 reference; continuous data remains in memory.
- News/calendar: Trading Economics metadata with provider and retrieval timestamps.
- Broker: read-only account, positions, and transaction/fill ingestion first.
- Discord: slash commands and image uploads calling this API.
- Storage: local private content-addressed files for chart evidence; PostgreSQL stores
  metadata and hashes. Object storage is a future deployment adapter.

The database contains order-intent and approval records so a later preview workflow can be
audited. Those records do not grant execution authority. Connector protocols intentionally
have no place, modify, cancel, close, or hedge methods.
