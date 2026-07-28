# Integration reliability and verification

Trading Agent has two deliberately separate verification classes:

- **Simulated** verification exercises the real adapter normalization and retry code against
  deterministic in-memory HTTP endpoints. It proves that the application contract works
  under known inputs. It does not prove credentials, provider availability, a connected
  MetaTrader terminal, DNS/TLS, or public webhook delivery.
- **Real** verification calls the configured read-only provider. Stored broker snapshots or
  authenticated inbound alerts are reported as `stored-real`; a successful current probe is
  reported as `real`. Simulated results are always labeled `simulated` and are never promoted
  into stored live-health evidence.

The deterministic simulator covers:

- OANDA account, positions, quotes, candles, and transaction pages;
- MetaTrader read-only health attestation, account, positions, quotes, candles, and events;
- Trading Economics calendar and news;
- an authenticated TradingView-shaped webhook with duplicate/replay behavior.

It also injects timeouts, connection resets, HTTP 429 responses, and truncated JSON pages.
OANDA, MetaTrader, and Trading Economics retry bounded transient failures. Truncated or
structurally invalid pages fail closed and are not ingested.

## Bounded soak run

Run the dependency-free simulated soak module:

```bash
python -m app.integration_soak --iterations 100 --max-seconds 30
```

Exercise recovery paths on every tenth iteration:

```bash
python -m app.integration_soak \
  --iterations 100 \
  --max-seconds 30 \
  --fault-every 10
```

Limits are enforced: 1–10,000 iterations and 0.1–300 seconds. The runner contacts no external
provider and prints JSON events plus a final summary. A zero exit status means the simulated
checks passed, not that live integrations are ready.

## Structured events

`app.services.observability` emits
`trading-agent.integration-event.v1` JSON objects with:

- UTC timestamp;
- stable event name;
- component and outcome;
- bounded caller-provided fields.

Sensitive field names such as authorization, password, token, secret, credential, and API key
are recursively redacted. Credential-shaped values are redacted in free text as a second
layer. Callers should still avoid sending raw HTTP headers or payloads to logging.

## Concurrency and ingestion

Broker synchronization uses a PostgreSQL advisory lock scoped to workspace, account, and
connection. Concurrent runs for the same feed fail quickly with
`BrokerSyncInProgressError`; different account feeds have different lock keys. After network
reads, the cursor is locked and compared again before ingestion, so a changed cursor causes a
retryable conflict rather than duplicate or out-of-order writes. Provider event IDs and
payload hashes preserve idempotency and expose conflicting replays.

The reliability suite runs concurrent contenders against that lock and sends deterministic
OANDA transaction data through the full normalization and PostgreSQL sync path.

## What still requires external verification

Before production use, all of the following remain real-world gates:

- OANDA: a real read-only API token and exact selected account ID;
- MetaTrader: a running MT4/MT5 terminal bridge, matching account, read-only attestation, and
  real execution history;
- Trading Economics: a valid API key and provider quota;
- TradingView: per-account webhook secret, public HTTPS on port 443/80 through the verified
  proxy path, TradingView 2FA, and an actual inbound test alert;
- network behavior: production DNS, certificate chain, proxy, firewall, provider rate limits,
  and regional availability.

Use the product's live integration verification after configuring those dependencies. Do not
interpret a simulated pass as evidence that any real provider or public endpoint is reachable.
