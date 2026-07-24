# Architecture

## Current milestone

The MVP is a human-in-the-loop journal and playbook service:

- FastAPI HTTP API and a minimal local interface.
- PostgreSQL locally through Docker or remotely through Neon.
- Deterministic position sizing.
- Structured trade plans and post-trade reflections.
- Optional OpenAI vision analysis with strict observation/hypothesis separation.
- Provider-neutral chat and vision with optional OpenAI and Anthropic adapters.
- A versioned runtime policy loaded at startup and checked by hooks before tool execution.
- Human-readable session names backed by internal UUIDs.

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

## Planned adapters

- Market data: provider to be selected.
- News/calendar: provider to be selected, with publication and event timestamps.
- Broker: read-only positions/fills first.
- Discord: slash commands and image uploads calling this API.
- Storage: object storage for chart evidence; PostgreSQL stores metadata and hashes.
