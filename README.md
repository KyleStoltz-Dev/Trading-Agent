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
