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
- Chart screenshot analysis through an optional OpenAI, Anthropic, or local Ollama adapter.
- Explicit separation of visible facts, hypotheses, missing evidence, and questions.
- Content-addressed chart evidence and provider/model/policy/prompt/input/output provenance.
- Idempotent fill imports, transaction cursors, account/position snapshots, and reconciliation.
- Immutable playbook versions, normalized rule evaluations, and sample-aware edge reports.
- Trading Economics calendar/news metadata with source and retrieval timestamps.
- A key-protected browser/API interface and OpenAPI documentation.
- Automatic economy/balanced/deep model routing with an in-session override.
- A confirmed development handoff that can change and test the agent in an isolated branch.

## Local setup

Requirements: Python 3.12+ and PostgreSQL. Model-backed chat and chart analysis can use an
OpenAI or Anthropic API key, or a token-free local Ollama model.

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

For local, token-free use on macOS:

```bash
brew install ollama
brew services start ollama
ollama pull qwen3.5:9b
```

Then set the following in `.env`:

```text
MODEL_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_ECONOMY_MODEL=qwen3.5:9b
OLLAMA_BALANCED_MODEL=qwen3.5:9b
OLLAMA_DEEP_MODEL=qwen3.5:9b
OLLAMA_CONTEXT_LENGTH=16384
```

Keep only one active `MODEL_PROVIDER` line. Ollama is restricted to the local machine by
default; using a remote host requires the explicit `OLLAMA_ALLOW_REMOTE=true` opt-in. On a
48 GB Apple Silicon Mac, start with the 9B model and measure responsiveness before trying a
larger model.

The default model remains the fallback for every route. Optionally give each effort profile
a different model:

```text
AGENT_MODE=auto
OPENAI_ECONOMY_MODEL=
OPENAI_BALANCED_MODEL=
OPENAI_DEEP_MODEL=
```

Blank profile values reuse `OPENAI_MODEL` (or `ANTHROPIC_MODEL`). Economy handles routine
logging and summaries, balanced handles normal analysis, and deep handles broad research,
backtesting, full-history analysis, and conflicting-rule work.

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
use `store=false`; Anthropic uses the stateless Messages API; local Ollama requests remain on
the configured Ollama host. Do not enter broker credentials
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
trading-agent develop --help
```

Sessions have predictable names. The default new-session name is the date, such as
`daily-2026-07-23`; duplicates receive `-2`, `-3`, and so on.

```bash
trading-agent --new --name gold-ny-review
trading-agent --session gold-ny-review
```

UUIDs remain available internally and can still be passed to `--session`, but are no longer
the primary interface.

### Develop the agent while using it

Use `/mode auto|economy|balanced|deep` to override routing for the current session. The
selected route is displayed with every reply.

When a clear software request appears in normal conversation—such as “change the agent so
it shows the active setup”—the CLI reiterates the requested change and asks once whether the
scope is correct. Confirmation starts the installed Codex CLI in a separate Git worktree.
Trading or strategy language alone does not trigger development mode. The explicit fallback
is:

```text
/develop add a command that compares two playbook versions
```

The coding run edits and tests after that one scope confirmation. It does not receive
`.env`, broker, database, OpenAI API, or Anthropic API credentials, and it cannot push,
merge, deploy, restart the running process, or add autonomous order execution.

Install and sign in to Codex separately, then set the repository path when the agent may be
started from another directory:

```bash
codex login
trading-agent health
```

```text
DEVELOPMENT_REPOSITORY=/absolute/path/to/Trading-Agent
```

Codex CLI can reuse a ChatGPT sign-in for this local coding workflow; trading chat and chart
analysis still use their separately configured API provider. Results stay on an isolated
local branch:

```bash
trading-agent develop start "add a session recap command"
trading-agent develop status SESSION_ID
trading-agent develop diff SESSION_ID
trading-agent develop approve SESSION_ID --yes
```

`approve` commits only on the isolated local branch. It never pushes or merges.
For a one-confirmation personal workflow, set
`DEVELOPMENT_APPROVAL_FLOW=scope_only`; validated changes are then committed to the
isolated branch automatically. The safer default, `scope_and_diff`, waits for the explicit
`develop approve` command.

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
