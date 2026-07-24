# Trading Agent

A provider-neutral, journal-first, human-in-the-loop trading copilot. It helps structure evidence, test a
playbook across market regimes, calculate risk, analyze chart screenshots, and review
execution. It does not autonomously place trades.

## What works in this MVP

- Migration-backed PostgreSQL plans, executions, snapshots, evidence, and reviews.
- Provider-neutral, read-only live-data contracts, a working OANDA v20 adapter, and MT5
  normalization examples.
- Bounded in-memory quotes/candles; the database does not retain every tick.
- Broker-contract-aware position sizing including spread, slippage, commission, quantity
  increments, margin, currency conversion, and a configured maximum risk.
- Context-timeframe and trigger-timeframe separation.
- Chart screenshot analysis through an optional OpenAI or Anthropic adapter.
- Explicit separation of visible facts, hypotheses, missing evidence, and questions.
- Content-addressed chart evidence and provider/model/policy/prompt/input/output provenance.
- Idempotent fill imports, transaction cursors, account/position snapshots, and reconciliation.
- Immutable playbook versions, normalized rule evaluations, and sample-aware edge reports.
- Trading Economics calendar/news metadata with source and retrieval timestamps.
- A key-protected browser/API interface and OpenAPI documentation.

## Local setup

Requirements: Python 3.12+, PostgreSQL, and an OpenAI or Anthropic API key for model-backed
chat and chart analysis.

```bash
cp .env.example .env
docker compose up -d postgres
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ai]"
uvicorn app.main:app --reload
```

Install only the provider you want when you do not need both:

```bash
pip install -e ".[dev,openai]"
# or
pip install -e ".[dev,anthropic]"
```

Choose the provider in `.env`:

```text
MODEL_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-5
```

or:

```text
MODEL_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-sol
```

`MODEL_PROVIDER=auto` works when exactly one provider key is configured. If both keys are
present, select one explicitly.

Generate a separate local API key; do not reuse a model or broker credential:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Save it as `TRADING_AGENT_API_KEY` in `.env`. The optional API will not start with a key
shorter than 32 characters.

Open:

- App: http://localhost:8000
- API documentation: http://localhost:8000/docs

The API and journal work without either model provider; model-backed chat and
`/api/charts/analyze` require a configured adapter.

## Interactive CLI

After installation, start the interactive agent:

```bash
trading-agent
```

Startup runs lightweight health checks, opens or resumes the latest locally persisted conversation,
and routes natural-language requests to the same application services used by the API. The
agent can calculate risk, inspect the journal, create a confirmed plan or reflection, analyze
a local chart path, and report system health. Journal mutations always require a terminal
confirmation. There are no broker execution tools.

Conversation turns are stored in PostgreSQL so a session can be resumed. Relevant recent
turns and requested journal/tool results are sent to the selected provider. OpenAI requests
use `store=false`; Anthropic uses the stateless Messages API. Do not enter broker credentials
or secrets into the conversation.

Every capability also remains available as an individual command:

```bash
trading-agent chat
trading-agent health
trading-agent risk --help
trading-agent plan
trading-agent chart /absolute/path/to/chart.png
trading-agent journal list
trading-agent review TRADE_ID
trading-agent sessions list
trading-agent db status
trading-agent db upgrade
trading-agent broker configure-oanda --help
trading-agent broker quote XAU_USD
trading-agent broker sync --help
trading-agent instrument configure --help
trading-agent instrument risk --help
trading-agent playbook version --help
trading-agent news sync --help
trading-agent edge report --minimum-sample 30
```

Sessions have predictable names. The default new-session name is the date, such as
`daily-2026-07-23`; duplicates receive `-2`, `-3`, and so on.

```bash
trading-agent --new --name gold-ny-review
trading-agent --session gold-ny-review
```

UUIDs remain available internally and can still be passed to `--session`, but are no longer
the primary interface.

## Runtime policy and hooks

Every CLI or API startup loads `app/trading-rules.json`. Interactive startup adds all rules
to the model instructions and records the policy version/hash in health output. Before every
model tool call, direct fallback command, or API service operation:

1. the policy hook verifies that the rules file has not changed since startup;
2. forbidden capabilities are rejected;
3. every exposed tool must have explicit read/mutate/deterministic metadata; and
4. model-requested mutations require terminal confirmation.

The current policy forbids broker order placement, modification, cancellation, hedging, and
position closing. Adding a future broker adapter does not grant the conversational agent
access; the policy and execution boundary must be reviewed separately.

The schema can audit a future order-preview and approval workflow, but the current
connector interfaces remain read-only and expose no broker write method.

The CLI calls Python services directly and does not require a local HTTP server. Run the
optional API/browser process only when needed:

```bash
trading-agent api --reload
```

Use `trading-agent api` for the browser interface, API clients, and future Discord/webhook
adapters.

## Data flow

Live quotes and candles remain in bounded process memory and must pass freshness/order
checks. PostgreSQL receives only durable decision and audit data: plans, broker executions,
fills, management events, snapshots, calendar/news metadata, evidence provenance, and
reviews. This keeps the journal useful without turning PostgreSQL into a tick database.

OANDA is currently the only complete live connector. It is read-only by construction. On a
new connection, `broker sync` starts at the current transaction cursor unless you explicitly
request a one-time historical start with `--from-transaction-id`.

Load the measurable starting strategy as an immutable version:

```bash
trading-agent playbook version \
  --name wyckoff-smc-fractal \
  --file docs/playbook-schema-v1.json \
  --hypothesis "Context plus lower-timeframe confirmation improves expectancy" \
  --minimum-sample 30
```

## Neon

Create a Neon project and replace `DATABASE_URL` in `.env` with its SQLAlchemy psycopg URL:

```text
postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

Never commit `.env`. Use a restricted development database until authentication and
per-user data isolation exist.

## Tests

```bash
ruff check .
pytest
```

## Current safety boundary

There is no broker order endpoint. Future broker integration begins with read-only
positions and fills. Any later order workflow must be previewed and explicitly confirmed
by the trader, with deterministic risk limits and an audit log.

See [the data model](docs/data-model.md), [the starting playbook](docs/playbook-v0.md), and
[architecture notes](docs/architecture.md). Before connecting accounts, read
[operations](docs/operations.md) and the [security model](docs/security.md).
