# Trading Agent

A journal-first, human-in-the-loop trading copilot. It helps structure evidence, test a
playbook across market regimes, calculate risk, analyze chart screenshots, and review
execution. It does not autonomously place trades.

## What works in this MVP

- PostgreSQL-backed trade plans and reflections.
- Deterministic position-size and planned-R calculations.
- Context-timeframe and trigger-timeframe separation.
- Chart screenshot analysis through the OpenAI Responses API.
- Explicit separation of visible facts, hypotheses, missing evidence, and questions.
- A minimal browser interface and OpenAPI documentation.

## Local setup

Requirements: Python 3.12+, Docker, and an OpenAI API key for chart analysis.

```bash
cp .env.example .env
docker compose up -d postgres
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open:

- App: http://localhost:8000
- API documentation: http://localhost:8000/docs

The API and journal work without `OPENAI_API_KEY`; only `/api/charts/analyze` requires it.

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
turns and requested journal/tool results are sent to the configured OpenAI model; API
responses are requested with `store=false`. Do not enter broker credentials or secrets into
the conversation.

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

Use `trading-agent --new` to start a clean conversation or
`trading-agent --session SESSION_ID` to select an older one.

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
