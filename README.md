# Trading Agent

A provider-neutral, journal-first, human-in-the-loop trading copilot. It helps structure evidence, test a
playbook across market regimes, calculate risk, analyze chart screenshots, and review
execution. It does not autonomously place trades.

## What works in this MVP

- PostgreSQL-backed trade plans and reflections.
- Deterministic position-size and planned-R calculations.
- Context-timeframe and trigger-timeframe separation.
- Chart screenshot analysis through an optional OpenAI or Anthropic adapter.
- Explicit separation of visible facts, hypotheses, missing evidence, and questions.
- A minimal browser interface and OpenAPI documentation.

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

The CLI calls Python services directly and does not require a local HTTP server. Run the
optional API/browser process only when needed:

```bash
trading-agent api --reload
```

Use `trading-agent api` for the browser interface, API clients, and future Discord/webhook
adapters.

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

See [the starting playbook](docs/playbook-v0.md) and
[architecture notes](docs/architecture.md).
