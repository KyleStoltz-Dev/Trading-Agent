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
- Persistent, trade-linkable mindset check-ins for readiness, risk acceptance, emotion tags,
  and pre-session, pre-trade, during-trade, or post-trade process notes.
- Trader profiles plus isolated per-strategy knowledge indexes for Discord, Telegram,
  X/Twitter, generic files, directories, and pasted notes.
- A manual backtest/forward-test evidence ledger with frozen strategy rules, explicit
  exclusions, expectancy, and feature-correlation reports.
- Deterministic candle measurements bridging visual review to numeric imbalance, equal-level,
  sweep-candidate, displacement, ATR, range, and change features.
- Trading Economics calendar/news metadata with source and retrieval timestamps.
- Startup calendar refresh and trade-intent event warnings when the news adapter is selected.
- Tiered research: local harness and journal first, read-only documented-domain fetch
  second, and optional broader Brave Search only when earlier sources are insufficient.
- A visible reference ledger for every response, covering runtime rules, harness files,
  journal records, charts, broker observations, news, calendar events, and web research.
- A key-protected browser/API interface and OpenAPI documentation.
- Automatic economy/balanced/deep model routing with an in-session override.
- Cross-platform Ollama resource checks that consider model size, loaded models, context
  headroom, available memory, swap pressure, reserve memory, and disk space before inference.
- Before-request cost estimates and after-response token/estimated-cost totals for known models;
  local Ollama correctly reports zero model API cost.
- A confirmed development handoff that can change and test the agent in an isolated branch.

## Local setup

Requirements: Python 3.12+ and PostgreSQL. Model-backed chat and chart analysis can use an
OpenAI or Anthropic API key, or a token-free local Ollama model.

Use the installer for your operating system:

```bash
# macOS
./install-trading-agent.command

# Linux (also works on macOS)
./install-trading-agent.sh
```

```powershell
# Windows PowerShell
Set-ExecutionPolicy -Scope Process Bypass
.\install-trading-agent.ps1
```

Each installer creates an isolated `.venv`, installs the same Python package with pip, and
starts the guided setup. Setup configures the provider, starts Ollama when selected, and
downloads the configured local model. It installs `trade` under `~/.local/bin` on macOS/Linux
and as a command file under `%LOCALAPPDATA%\TradingAgent\bin` on Windows; it never collects
API keys, broker tokens, or database passwords. The default installer includes both hosted
model adapters, so choosing OpenAI or Anthropic does not leave a missing SDK.
The former `install.command`, `install.sh`, and `install.ps1` names remain thin compatibility
wrappers.
After that, start the agent from any directory:

```bash
trade
```

If the launcher directory is not already on `PATH`, setup prints platform-specific
instructions. The longer manual installation remains available:

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

For local, token-free use, install Ollama for your operating system from
[ollama.com/download](https://ollama.com/download). On macOS, Homebrew is also supported:

```bash
brew install ollama
brew services start ollama
ollama pull qwen3.5:9b
```

On Linux, follow Ollama's Linux installer and run `ollama serve` if the service is not active.
On Windows, launch the Ollama desktop application. PostgreSQL can be kept consistent across
all three platforms with `docker compose up -d postgres`; native PostgreSQL installations are
also supported. Setup gives OS-specific guidance and does not assume Homebrew outside macOS.

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
48 GB Apple Silicon Mac, use 9B for economy/routine work and the 24-GB 35B-A3B model for
balanced/deep analysis when its extra latency is worthwhile:

```bash
trade models list
trade models pull qwen3.5:35b-a3b
trade models use qwen3.5:35b-a3b --tier quality
```

Inside chat, `/model` shows local profiles, `/model use qwen3.5:35b-a3b` creates a
session-only override, and `/model auto` restores tier routing. `/mode` still controls
reasoning effort. Chart analysis accepts `--model qwen3.5:35b-a3b --reasoning-effort high`.
Before local inference, the resource guard recalculates whether the selected model fits the
current machine. It can warn, refuse an unsafe explicit override, or route an automatic
request to the configured smaller model. The check uses OS-neutral telemetry on macOS,
Linux, and Windows. A remote Ollama server is not judged using the client computer's memory.

`trade setup` can safely change the selected provider later. It rewrites only non-secret
provider settings, collapses duplicate provider entries, and never reads or writes API keys.
Configuration is discovered from `TRADING_AGENT_CONFIG`, the editable installation, the
standard user configuration directory, or the current directory.

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

API journal reads and writes require `X-Strategy-Version` with one immutable strategy-version
UUID. Before a mutating request, an authenticated client requests a short-lived token from
`POST /api/confirmations/challenge`, binding it to the exact method, path, and request-body
SHA-256. Send that token once as `X-Trader-Confirmation`; replay and request substitution are
rejected. This protects request intent but is not a second identity factor, so keep the API
on loopback unless separate TLS and network controls are configured.

Open:

- App: http://localhost:8000
- API documentation: http://localhost:8000/docs

The API and journal work without either model provider; model-backed chat and
`/api/charts/analyze` require a configured adapter.

## Interactive CLI

After installation, use the short command:

```bash
trade
```

`trading-agent` remains an equivalent compatibility command. Startup checks local services,
warms a local model with a tiny generation test, opens or resumes the latest persisted
conversation, and routes natural-language requests to the same services used by the API.
The agent can calculate risk, inspect the journal, create a confirmed plan or reflection,
analyze a local chart path, and report system health. Journal mutations always require a
terminal confirmation. There are no broker execution tools.

Replies render as normal terminal Markdown instead of raw Markdown inside a large panel.
Before a model call, the CLI shows the selected context, model route, and estimated cost,
then displays a thinking/tool-activity spinner. After the reply it shows provider-reported
token usage, estimated API cost when known, and the complete reference ledger. Useful chat
commands are:

```text
/examples             show starter requests
/cost                 show configured model prices
/sources              show references used for the last response
/context              show selected local harness files
/mode auto|economy|balanced|deep
/model                show installed/configured local models
/model use NAME       override the local model for this session
/model auto           restore automatic model-profile routing
```

Conversation turns are stored in PostgreSQL so a session can be resumed. Strategy
conversations are tagged with the exact immutable playbook version that was active when each
turn was created. Only general history or history from the host-selected strategy version is
eligible for a new prompt; changing from ICT to Wyckoff does not carry ICT turns into the
Wyckoff context. Relevant journal/tool results are also scoped before they are sent to the
selected provider. OpenAI requests use `store=false`; Anthropic uses the stateless Messages
API; local Ollama requests remain on the configured Ollama host. Do not enter broker
credentials or secrets into the conversation.

Every capability also remains available as an individual command:

```bash
trading-agent chat
trading-agent health
trading-agent health --model-smoke-test
trading-agent setup --help
trading-agent onboard
trading-agent integrations
trading-agent risk --help
trading-agent plan
trading-agent preflight --help
trading-agent chart /absolute/path/to/chart.png
trading-agent journal list
trading-agent review TRADE_ID
trading-agent mindset check --help
trading-agent mindset list
trading-agent sessions list
trading-agent db status
trading-agent db upgrade
trading-agent broker configure-oanda --help
trading-agent broker quote XAU_USD
trading-agent broker sync --help
trading-agent instrument configure --help
trading-agent instrument risk --help
trading-agent playbook version --help
trading-agent strategy create --help
trading-agent strategy list
trading-agent strategy use wyckoff-pure
trading-agent knowledge import --help
trading-agent knowledge search --help
trading-agent knowledge exclude ITEM_UUID --strategy wyckoff-pure
trading-agent knowledge restore ITEM_UUID --strategy wyckoff-pure
trading-agent experiment start --help
trading-agent experiment report EXPERIMENT_ID
trading-agent news sync --help
trading-agent edge report --minimum-sample 30
trading-agent develop --help
```

The UUID-based knowledge commands are an administrative fallback. In interactive chat,
use natural language such as “find the ICT notes in my active Wyckoff strategy” or
“restore the note about the London session.” The agent first shows bounded, numbered
matches with short `knowledge-…` references and source previews. It can change only one
exact returned item after terminal confirmation. “Remove” quarantines the item from model
retrieval; it does not delete the source or audit record. The model cannot select a strategy
UUID, use a wildcard, or change an item outside the session’s active immutable strategy.

Sessions have predictable names. The default new-session name is the date, such as
`daily-2026-07-23`; duplicates receive `-2`, `-3`, and so on.

```bash
trading-agent --new --name gold-ny-review
trading-agent --session gold-ny-review
```

UUIDs remain available internally and can still be passed to `--session`, but are no longer
the primary interface.

Trade plans receive similarly predictable references, such as
`xauusd-20260725-ny-short-1`. Use the reference with `journal show` or `review`.
Backtest and forward-test commands accept the experiment name when it is unique. Add
`--show-internal-ids` to session or journal lists only when debugging an internal relationship.

### Unified pre-trade workflow

Use the guided workflow before deciding whether to take a trade:

```bash
trade preflight
```

Inside `trade`, explicit near-term requests such as “Should I take this trade?” offer to
launch the same workflow with a default-yes prompt. Declining returns to normal chat without
creating an assessment. A validation error is contained and also returns to chat.

It requires a named session with one exact active strategy version, then walks through the
planned entry, stop, target, thesis, invalidation, direct observations, labeled hypotheses,
the selected setup's requirements and exclusions, deterministic risk sizing, readiness,
predefined-risk acceptance, emotion tags, and current news/calendar freshness. Add
`--live-market` to include a read-only OANDA quote and measured recent-candle features when
OANDA is configured.

The result is `eligible`, `conditional`, `stand aside`, or `blocked`, with separate strategy,
risk, mindset, evidence, and news completeness scores. These scores grade adherence to the
selected rules; they are not a win-probability forecast, individualized financial advice, or
permission to execute. The trader still makes the final choice.

Every completed run stores an auditable assessment and its pre-trade mindset check-in in
PostgreSQL. A proceed choice also creates and links a journaled trade plan. A stand-aside or
cancel choice records the reasoning without creating a plan. The workflow never sends,
modifies, or cancels a broker order.

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
It runs with isolated ephemeral home/configuration directories outside the repository, copies
only the Codex authentication needed for the run, deletes that copy afterward, and rejects
tracked private environment files. A host-side diff scan rejects known broker-order methods
and write endpoints before the change can reach review or approval.

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

## Progressive trading harness

`app/harness/HARNESS.md` is a compact role map. For each request, deterministic routing selects
only matching workflows and references from `skills/`, `market-models/`, `psychology/`, and
`references/`. The selected files and hashes are placed in model context; unrelated material
is not loaded. Use `/context` to see what influenced the previous response.

Harness content supplies task context only. It cannot replace `app/trading-rules.json`, bypass
execution hooks, perform risk arithmetic, or add broker execution capability.

## Tiered research and citations

The agent resolves evidence in a fixed order:

1. Local runtime policy, task-specific harness files, and stored journal/evidence records.
2. Configured timestamped broker/news connectors and full-page reads from explicitly
   allowlisted documented domains.
3. Optional broad web search only when the earlier tiers cannot answer.

Every selected harness file is identified by path and content hash. Every tool result that
influences a response adds a source locator and retrieval/market timestamp when available.
The CLI appends this ledger to every answer, so source disclosure does not depend on the
model remembering to format citations. When recent conversation turns are included in a
prompt, their exact serialized content is represented by a content hash in the ledger too.

Allowlisted page fetching is read-only, bounded by time and size, rejects credentials and
non-public/private-network destinations, and revalidates redirects. Configure the domains:

```text
WEB_FETCH_ENABLED=true
WEB_FETCH_ALLOWED_DOMAINS=oanda.com,tradingview.com,cmegroup.com,federalreserve.gov,fred.stlouisfed.org,bls.gov,bea.gov,tradingeconomics.com
WEB_FETCH_ALLOWED_PATHS=oanda.com=/,/us-en/,/rest-live-v20/;federalreserve.gov=/,/newsevents/,/monetarypolicy/
```

Each tier-2 request, including every redirect, displays the exact GET URL for confirmation.
The path policy is domain-specific and rejects query strings, encoded paths, secret/private
path markers, high-entropy path segments, and paths outside the configured documentation
prefixes before prompting or connecting. The connection is made to the already validated
public IP while retaining the original TLS SNI and Host header, preventing a second DNS lookup
from changing the destination between validation and connection. An allowlisted public server
can still proxy content internally, so fetched responses remain untrusted evidence.

Tier-3 search is disabled by default. To enable provider-neutral search for OpenAI,
Anthropic, or Ollama, add a Brave Search API key:

```text
WEB_SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=...
```

Search snippets and fetched pages are untrusted evidence, never executable instructions.
A search result's full page is not fetched unless its domain is also on the allowlist.
Imported notes, journal text, broker/news payloads, fetched pages, and search results are
delimited as untrusted evidence and cannot select the active strategy, alter policy, authorize
a mutation, or redefine tool permissions. This reduces prompt-injection risk; it is not a
claim that arbitrary external text can be made inherently trustworthy.

Tier-3 search also requires a separate confirmation that displays the exact normalized
query, provider, destination, and reason the local/allowlisted tiers were insufficient.
Declining makes no search request. Secret-like queries are rejected before confirmation or
network access. Allowlisting a domain permits bounded reads from that destination; it never
permits credentials, private journal text, or other secrets to be placed in a URL or search
query.

## Model cost display

`/cost` shows configured economy, balanced, and deep models. The CLI estimates the first
response before sending it and reports provider-supplied usage after all model/tool rounds.
The built-in price table uses the official
[OpenAI model comparison](https://developers.openai.com/api/docs/models/compare) and
[Claude Sonnet 5 announcement](https://www.anthropic.com/news/claude-sonnet-5), and currently
covers GPT-5.6 Sol, Terra, Luna, and Claude Sonnet 5. Unknown models display
`pricing unavailable` rather than guessing.
Provider billing remains authoritative; Brave Search billing, hardware, and electricity are
not included.

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
reviews, mindset check-ins, and strategy-scoped conversation turns. Imported strategy
material is stored in a separately scoped knowledge index tied to one immutable strategy
version. `knowledge exclude` quarantines a suspect item from retrieval without deleting its
audit record; `knowledge restore` returns it only to that same strategy version. This keeps
the journal useful without turning PostgreSQL into a tick database.

OANDA is currently the only complete live connector. It is read-only by construction. On a
new connection, `broker sync` starts at the current transaction cursor unless you explicitly
request a one-time historical start with `--from-transaction-id`.

### Database schema upgrades

In this project, a database migration is a versioned change to the tables, columns, indexes,
or constraints in the same PostgreSQL database. It does not mean moving the dataset to
another database. After pulling a version that adds storage such as strategy-scoped
conversation turns or pre-trade assessments, run:

```bash
trade db status
trade db upgrade
```

The upgrade applies only pending structural changes and preserves existing journal data.
Changing from local PostgreSQL to Neon is a separate, explicit configuration and data-transfer
decision.

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

## Implemented boundaries

Implemented now:

- PostgreSQL journaling, strategy-scoped conversations and knowledge, mindset check-ins,
  an auditable guided pre-trade assessment, deterministic risk sizing, screenshot analysis,
  OANDA read-only market/account data, Trading Economics calendar metadata, and tiered cited
  research.
- Frozen manual experiment records for backtest and forward-test observations. These records
  calculate reports from entered samples; they are not an automated historical replay engine
  or paper-trading monitor.

Adapter-only or planned:

- MT5 currently provides normalization examples, not a live terminal or Expert Advisor
  bridge. cTrader, Interactive Brokers, and Finnhub are planned integrations.
- Bulk attachment downloading, OCR, video/PDF ingestion, automatic screenshot captioning,
  background desktop notifications, and hosted multi-user isolation are not implemented.
- There is no broker order endpoint and no autonomous execution. Any future order workflow
  must be separately reviewed, previewed, explicitly confirmed by the trader, constrained by
  deterministic risk limits, and recorded in an audit log.

See [the data model](docs/data-model.md), [the starting playbook](docs/playbook-v0.md),
[the knowledge, strategy, and testing guide](docs/knowledge-strategies-testing.md), and
[architecture notes](docs/architecture.md). Before connecting accounts, read
[operations](docs/operations.md) and the [security model](docs/security.md).
