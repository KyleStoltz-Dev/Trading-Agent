# Operations

## Startup

Normal daily startup:

```bash
trade
```

If you want one-command startup from the repository, run:

```bash
bash scripts/start-trading-agent.sh --auto
```

That helper:

1. Creates `.env` from `.env.example` if missing.
2. Resolves local dependencies and starts PostgreSQL via `docker compose` (if available).
3. Waits for required local services to be reachable.
4. Runs `trade quickstart` and opens a fresh Trading Agent chat session.
5. Fails fast with a clear message if required local `.env` keys are missing (`POSTGRES_PASSWORD` or `DATABASE_URL`).

If your `.env` is already prepared, this is your shortest valid flow.

Use `--help` to see extra switches (`--no-chat`, `--name`, etc.).

You can skip chat and just verify readiness with:

```bash
bash scripts/start-trading-agent.sh --no-chat
```

One-time guided setup:

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

The installers bootstrap a pinned `uv`, then synchronize the checked-in lock file. The
Windows installer also includes the locked MetaTrader 5 bridge dependency.

Or, after installation:

```bash
trade setup
```

For a faster non-interactive path, use:

```bash
trade quickstart
trade quickstart --provider openai --broker oanda --news none
```

Detailed diagnostics and the compatibility command remain available:

```bash
source .venv/bin/activate
trading-agent health --strict
trading-agent
```

For token-free local inference, install Ollama from
[its OS-specific download page](https://ollama.com/download). macOS can alternatively use:

```bash
brew install ollama
brew services start ollama
ollama pull qwen3.5:9b
```

Linux can use Ollama's Linux installer and `ollama serve`; Windows uses the Ollama desktop
application. For PostgreSQL, set a unique `POSTGRES_PASSWORD` in the private mode-`0600`
`.env` before running `docker compose up -d postgres`. Compose publishes PostgreSQL only on
`127.0.0.1` by default. Native installations remain supported.

### Rotate an existing local PostgreSQL password

Changing `POSTGRES_PASSWORD` after a PostgreSQL data directory or Docker volume has already
been initialized does not change the password stored by PostgreSQL. Rotate the role
interactively so the new password is not placed in shell history:

```bash
psql -d postgres
\password trading
\q
```

Then update `POSTGRES_PASSWORD` and `DATABASE_URL` in the one private Trading Agent `.env`
file, keep that file at mode `0600`, and restart the agent. For Docker, run the same
`\password` command inside the existing database container before changing `.env`; recreating
the container without deliberately preserving or migrating the volume can destroy local data.

Set `MODEL_PROVIDER=ollama` and `OLLAMA_MODEL=qwen3.5:9b` in `.env`. The health command checks
that Ollama is reachable and that the configured model is installed. If the service was not
registered successfully, run `ollama serve` in a separate Terminal window for that session.
Run `trading-agent health --model-smoke-test` to require a real generated response.

Manage local model tiers without manually editing `.env`:

```bash
trade models list
trade models pull qwen3.5:35b-a3b
trade models use qwen3.5:35b-a3b --tier quality
```

`quality` changes balanced and deep only, leaving default/economy on the faster model.
Persistent changes require a restart; `/model use NAME` is an immediate session-only override.
`/model unload` releases the current session's model immediately. By default, Ollama model
weights also expire after two idle minutes and are released when chat exits; configure
`OLLAMA_KEEP_ALIVE` and `OLLAMA_UNLOAD_ON_EXIT` when a different residency policy is needed.
The health smoke test uses a one-shot model load and does not leave weights resident.
Before every `ollama pull`, the agent estimates the download from a parameter count in the
model tag and requires download-plus-verification headroom on the filesystem that stores
Ollama models. For an ambiguous tag such as `latest`, provide a conservative size:

```bash
trade models pull vendor:model-latest --expected-size-gb 20
```

The CLI talks directly to Python services; `trading-agent api` is optional. Startup loads
and hashes the runtime rules, checks PostgreSQL and migration state, checks configured
providers/connectors, and resumes the latest named conversation unless told otherwise.
When enabled, startup tries a configured Homebrew service on macOS or starts `ollama serve`
directly on Linux/Windows when Ollama is installed but unreachable. PostgreSQL service names
vary across Linux distributions and Windows installations, so the agent gives platform-
appropriate Docker/native guidance instead of guessing or requesting administrator access.
It never installs a system package silently and never collects secrets.

On first start, or whenever the trader profile/integrations need changing, run:

```bash
trade onboard
trade integrations
```

Both setup wizards show numbered choices with plain-language descriptions. They accept the
number, displayed provider name, or common aliases such as `Open AI`, `OANDA v20`, and
`Trading Economics`. Invalid entries are explained and prompted again instead of producing a
traceback. Onboarding validates timezones and risk percentages, normalizes common market and
session names, suggests likely typo corrections, and displays one complete review table before
writing the profile or integration selections.

Profile answers are written only after the final confirmation to the `trader_profiles` table in
PostgreSQL. Broker and news provider selections are written to the private `.env`; credentials
remain separate. The incomplete wizard is not placed in conversation history or sent to a model.
Onboarding always starts from clean, experience-aware recommendations. Previously saved answers
are not displayed or reused in prompt brackets; the saved profile remains unchanged until the
new final review is confirmed.
At the final review, answering No opens an edit menu rather than exiting. One field is changed
at a time and the complete review is shown again. Exiting without writing requires choosing
`Discard onboarding` and confirming that separate action.

Beginner setup recommends the computer's detected timezone, a guided curriculum, one practice
instrument and session, a simple predefined-risk style, process goals, and 0.5% planned risk.
Personal/demo accounts collect only name, starting size, and currency; the advanced loss,
drawdown, news, and holding questionnaire is skipped. Beginner prop setup can defer unverified
firm rules or enter only the essential loss, target, and drawdown values.

The same final confirmation writes one active `account_constraint_profiles` record when an
account is configured. This record is independent of `trading_accounts`: the former contains
trader-entered personal/prop program rules, while the latter identifies a broker account used
for imported execution data. Account-rule profiles contain no broker credentials. Rerun
onboarding and edit `Account and prop rules` to switch the active reminder profile.

Prop setup records firm/program, evaluation phase, starting size/currency, loss and profit
limits, trading-day and consistency limits, drawdown calculation, news/overnight/weekend
policies, reset timezone, and bounded custom restrictions. `unknown` is a valid state and is
shown as a verification gap. A preflight audit links to the exact account-rule profile used.
Stored percentages are translated to currency amounts from starting size for readability, but
the application does not treat starting size as current equity.

Onboarding also offers guided, flexible, on-demand, or paused teaching. Every level can select
the full topic library and ask questions at any time. After a successful `trade onboard`, the
interactive agent opens automatically. Use `/onboard` to update setup without leaving the
session, `/mode` to change model effort, `/learn` for curriculum, and `/strategy` to change
isolated strategy context. Natural-language preference changes repeat the full proposed
selection and require confirmation before the database is updated.

```bash
trade learn
trade learn start lesson-probability-and-process
```

Inside chat, `/learn` shows progress and `/learn LESSON` begins a sourced teaching request. See
[`learning.md`](learning.md) for source tiers and the education/execution isolation boundary.

When Trading Economics is selected and configured, startup refreshes the upcoming calendar.
Trade-intent requests receive a nearby-event warning and timestamped event context.

## Configuration discovery

Configuration lookup order is:

1. an absolute `TRADING_AGENT_CONFIG` when explicitly set;
2. `~/.config/trading-agent/.env`;
3. `.env` beside an editable source installation.

Exactly one file is loaded; files are never merged and the current directory is never searched.
Environment variables override the selected file. On POSIX, the file must be current-user
owned, non-symlink, and mode `0600`. `trade setup` updates the active file atomically and
refuses to write secret keys.

## Harness context

Every response loads the compact `HARNESS.md` entry point and at most four matching task
resources within the configured context budget. `/context` shows the selected relative paths.
Routing is deterministic and tested against representative chart, planning, risk, review,
psychology, regime, and edge-analysis prompts.

## Mindset check-ins

Use a short check-in to record process readiness and whether the predefined risk is genuinely
accepted. It is a journal aid inspired by probabilistic, process-first trading discipline—not a
mental-health diagnosis and not a trade signal.

```bash
trade mindset check --phase pre-trade --readiness 4 --accepted-risk \
  --emotion focused --emotion patient \
  --emotional-state "I feel calm and fully accept the loss." \
  --trade xauusd-20260725-ny-short-1 \
  --note "The predefined loss is acceptable."
trade mindset list --limit 10
```

`--emotion` is a repeatable, concise tag used for later grouping. `--emotional-state`
preserves the trader's exact free-form wording, including profanity. It is reflective evidence,
not a trade signal. Database writes still show the exact proposed change and require confirmation
unless `--yes` was explicitly supplied.

Valid phases are `pre-session`, `pre-trade`, `during-trade`, and `post-trade` in the CLI
(Typer also accepts their underscore forms). A check-in may reference a trade by its readable
reference or internal UUID. Agent-created check-ins require explicit confirmation; recent
check-in retrieval is read-only.

The normal response footer stays compact. Model prose is normalized for terminal display:
document-style Markdown fences are unwrapped, wide tables are stacked vertically, unsafe control
characters are removed, and runnable command/code fences remain intact. Hosted providers receive
the same economy/balanced/deep output budget used by the cost estimate. Ollama adds bounded
reasoning headroom and performs one non-thinking retry if reasoning consumes the generation
without producing visible text. `/details` expands route,
token usage, cost, local
performance, context count, and reference count. `/sources` shows the full reference ledger
for the previous answer. It always includes the runtime policy and selected harness resources,
then adds journal records, charts, broker observations, news/calendar items, fetched pages,
and search results actually used. `/context` lists the exact harness resources.

At chat startup, `Recall` is assembled deterministically from PostgreSQL and shown locally. It
contains the trader's saved goals, the exact active immutable strategy version, metadata for the
most recent prior session in that scope, up to three unresolved plans, two recent review scores,
and three structured mindset check-ins. It never includes raw prior turns, reflection notes,
mindset notes, or free-form emotional-state prose. Use `/memory` to inspect the complete bounded
set. Use `/memory use` to confirm sending it to the configured model with the next request only;
the consent resets after that response or a strategy switch. `/memory off` cancels it.

## Model routing

`AGENT_MODE=auto` classifies each message locally before any provider request. Routine
journal operations use economy effort, normal analysis uses balanced effort, and broad
research uses deep effort. In chat, use `/mode` to override this for the rest of that
session. The route and model are printed with every reply.

The CLI also prints a rough first-response estimate before sending an API request and the
provider-reported total afterward. `/cost` shows the configured model tiers. Estimates use
an approximate preflight token count; final provider billing is authoritative. Ollama shows
zero API cost but does not estimate electricity or hardware cost.

All three routes fall back to the provider's base model. Set the matching
`OPENAI_*_MODEL`, `ANTHROPIC_*_MODEL`, or `OLLAMA_*_MODEL` values only when you intentionally want separate
models. Risk and position sizing never use this router; they remain deterministic.

## Development handoff

Set `DEVELOPMENT_REPOSITORY` to the absolute clone path when the agent may start outside
the repository. Confirm Codex is installed and signed in:

```bash
codex --version
codex login status
trading-agent health
```

Host Codex development is deliberately disabled by default. It is available only under
`APP_ENV=development` and requires:

```text
DEVELOPMENT_ENABLED=true
DEVELOPMENT_ACKNOWLEDGE_HOST_FILESYSTEM_READ_RISK=true
```

That acknowledgment is not a claim of secure isolation. Codex `workspace-write` limits
writes, not filesystem reads, and it is not a container boundary. Codex or child tools may
read other host-accessible files, including the ephemeral copy of Codex authentication.
Enable it only for a trusted machine, repository, and requested change.

In interactive chat, state a clear software change or use `/develop`. After the scope
confirmation, the command may run for several minutes. It records a local session under
`.data/development`, creates an `agent/dev-*` branch in a separate worktree, runs Codex
non-interactively with workspace-only writes, and then performs non-executing diff scans.
Codex may run Ruff and pytest inside its workspace sandbox, but the host application never
executes generated project code before review.

Inspect and commit the result:

```bash
trading-agent develop status SESSION_ID
trading-agent develop diff SESSION_ID
trading-agent develop approve SESSION_ID --yes
```

Approval reruns the broker-write, secret, sensitive-path, binary, and symlink scans and
commits only to the isolated branch. Push, pull-request,
merge, deployment, and process restart are deliberately separate operations.

`DEVELOPMENT_APPROVAL_FLOW=scope_only` removes the second commit approval: the initial
reiterated-scope confirmation authorizes Codex to edit, validate, and commit on the isolated
branch. `scope_and_diff` is the default for shared installations.

Review the full diff before approval. Run dependency, build-hook, test-runner, or other
executable changes in a disposable environment after review.

Codex runs with redirected ephemeral home, XDG, Codex, Git-config, and temporary directories
outside the repository. Only its authentication file is deliberately copied into that
runtime, and the copy is removed afterward. This redirection reduces accidental exposure but
does not prevent reads from other host paths or prevent child tools from reading the staged
authentication. Tracked `.env`/`.env.save` files stop the handoff. A host-side diff scan also
rejects known broker-order SDK calls, write endpoints, credential-shaped additions, sensitive
file paths, binaries, and symlinks before review or approval.

## Database

Inspect and upgrade:

```bash
trading-agent db status
trading-agent db upgrade
```

If a pre-Alembic database is detected, normal upgrade fails closed. Adopt it only after
choosing a new absolute backup path:

```bash
trading-agent db adopt-legacy \
  --backup /absolute/safe/path/trading-agent-before-adoption.dump \
  --yes
```

The command creates a restricted `pg_dump`, transactionally moves the legacy tables,
applies migrations, copies rows, verifies counts, and removes the temporary legacy schema
only after verification succeeds.

## OANDA read-only sync

Put `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, and `OANDA_ENVIRONMENT=practice` in `.env`.
Then:

```bash
trading-agent broker configure-oanda --label practice --currency USD
trading-agent broker quote XAU_USD
trading-agent broker sync
```

The first sync intentionally starts from OANDA's current transaction cursor instead of
silently importing an unbounded history. To import history on a new connection, explicitly
provide the transaction immediately before the desired range:

```bash
trading-agent broker sync --from-transaction-id 12345
```

Once a cursor exists, rewind is refused. Transaction imports are idempotent. Every sync
also records account/position snapshots and compares broker positions with the normalized
fill ledger only when complete history coverage is proven. Baseline/incremental coverage
remains explicitly unreconciled instead of reporting a false mismatch for positions opened
before the cursor.

OANDA fill transactions can contain separate `tradeOpened`, `tradeReduced`, and
`tradesClosed` effects. The sync applies those explicit effects to lifecycle status and
`closed_at`, retains all normalized effects on the execution event, and records
provider-reported commission, financing, guaranteed-execution fee, and half-spread cost on
the fill. It never infers an opening transaction that is outside the selected history range.

OANDA positions are instrument aggregates. A position snapshot is linked to a lifecycle trade
only when exactly one active imported trade matches that account and instrument. With hedging
or multiple same-instrument trades, the snapshot deliberately remains unlinked rather than
choosing the wrong trade.

## MetaTrader 4/5 read-only sync

The Trading Agent process never loads a trading terminal or exposes an order method. It calls
a fixed read-only bridge contract. Configure the client in `.env`:

```text
BROKER_PROVIDER=metatrader
METATRADER_PLATFORM=mt5
METATRADER_BRIDGE_URL=https://YOUR-PRIVATE-BRIDGE
METATRADER_BRIDGE_TOKEN=generate-at-least-32-random-characters
METATRADER_ACCOUNT_ID=12345678
METATRADER_MODE=practice
```

Then verify and register the exact account before importing:

```bash
trade broker configure-metatrader --label mt5-practice
trade broker quote XAUUSD
trade broker sync
```

The first sync creates a present-time cursor and imports no unbounded history. On a new
connection, explicitly choose the beginning of a history import:

```bash
trade broker sync --from-cursor 2020-01-01T00:00:00Z
```

Each response is bounded to 5,000 deals and each terminal query is restricted to an adaptive
time window. When a response reports more history, rerun sync to advance the stored cursor;
`has_more` is preserved in the command result. Final exits are checked against the exact MT5
position history so a trade that closes on an earlier page is not left partially closed.
Imports are idempotent. A reused event ID with changed normalized content holds the cursor and
marks the connection degraded. Quotes and requested candles remain bounded in memory;
executions, fills, account snapshots, and net position snapshots are durable.

The included MT5 companion requires Windows and the official terminal. MT4 must implement the
same read-only contract from an EA/bridge. See `docs/metatrader-bridge.md`.

## Instrument contract and sizing

Create a JSON instrument specification from the broker's current contract details, then:

```bash
trading-agent instrument configure --file examples/oanda-xauusd-spec.json
trading-agent instrument risk --help
```

Sizing includes stop distance, spread, slippage, round-trip commission, quantity step,
minimum/maximum quantity, margin, conversion rate, and the configured maximum risk. The
stored specification is versioned, so an old plan keeps the assumptions used at the time.

## News and calendar

For the free calendar, set `NEWS_PROVIDER=forex-factory`; no API key is required. For
Trading Economics, set `NEWS_PROVIDER=trading-economics` and
`TRADING_ECONOMICS_API_KEY`. Then:

```bash
trading-agent news sync --help
trading-agent news upcoming --hours 24 --currencies USD --details
trading-agent news history "Core PCE" --currency USD --limit 6
trading-agent news watch --currencies USD --alert-minutes 60 --yes
```

Only provider metadata and summaries are retained: external ID, original timestamps,
retrieval timestamp, importance, country/category/symbol, values, and source URL. Provider
entitlements and redistribution terms still apply. Forex Factory supplies the current weekly
economic calendar only; it does not supply headline metadata, and its export may not provide
post-release actual values. A missing actual remains visibly pending.

## Tiered web research

Full-page retrieval is enabled only for `WEB_FETCH_ALLOWED_DOMAINS`. Edit the comma-separated
list in `.env`, restart `trade`, and use `/health` to confirm the capability. Requests are
read-only and bounded; redirects outside the allowlist fail closed.

Broader discovery is a separate tier and stays disabled unless configured:

```text
WEB_SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=your-dedicated-search-key
WEB_SEARCH_MAX_RESULTS=5
```

Restart after changing these values. `trade health --strict` reports an error if Brave is
selected without its key. Search only returns titles, URLs, and snippets; full-page reading
still requires explicitly adding the result's domain to the fetch allowlist.

## Evidence and analytics

Chart analysis stores the original image by SHA-256, provider/model, policy/prompt/input/
output hashes, output JSON, and normalized facts versus hypotheses.

```bash
trading-agent chart /absolute/path/chart.png \
  --instrument XAUUSD --venue OANDA --timeframe M5
trading-agent chart --clipboard \
  --instrument XAUUSD --venue OANDA --timeframe M5
trading-agent edge report --minimum-sample 30
```

`--clipboard` reads the image currently copied from a chart, browser, or image viewer. It
accepts only PNG, JPEG, or WebP image bytes up to 10 MB and never interprets clipboard text as
a filename. macOS uses its built-in clipboard support; Windows uses PowerShell; Linux requires
`wl-paste` from `wl-clipboard` or `xclip`. If no supported image or reader is available, the
command explains what to copy or install. Provide either a path or `--clipboard`, never both.
Hosted providers still show the exact provider, destination, image source/type/size, and context
for confirmation before any bytes leave the machine. Accepted images enter the same
content-addressed evidence and analysis-provenance pipeline as path-based screenshots.

Expectancy remains explicitly unvalidated below the minimum sample. Process score and
outcome are kept separate.

## Imported knowledge and strategy tests

Create each methodology separately before importing material:

```bash
trade strategy create \
  --name wyckoff-pure \
  --file examples/strategies/wyckoff-pure.json \
  --minimum-sample 30
trade knowledge import /absolute/path/to/export.zip --strategy wyckoff-pure
trade knowledge search "spring reclaim" --strategy wyckoff-pure
```

Use a separate explicit definition for ICT or any combined methodology. A conversation can
activate only one exact version:

```bash
trade strategy use wyckoff-pure --session SESSION_NAME
```

Backtest/forward-test observations are frozen to that definition hash:

```bash
trade experiment start --help
trade experiment sample "XAUUSD NY replay 2024" --file /absolute/path/to/sample.json
trade experiment report "XAUUSD NY replay 2024"
trade experiment complete "XAUUSD NY replay 2024"
```

See `docs/knowledge-strategies-testing.md` for supported Discord, Telegram, X, generic file,
screenshot, numeric-feature, and daily/weekly outlook workflows.

## Recovery

- Keep `.env` and backups outside Git.
- Test restores periodically with the source-read-only verification command:

  ```bash
  uv run python scripts/verify_postgres_backup_restore.py
  ```

  It restores into a random temporary database, compares table counts and migration revisions,
  and never writes to the source. See [`release.md`](release.md) for retained backups, the
  disposable migration drill, release gates, and rollback instructions.
- If reconciliation becomes degraded, stop relying on derived position state, verify the
  configured account and cursor, and compare broker transactions against imported fills.
- Evidence is file-backed. Back up both PostgreSQL and `.data/evidence` to preserve a
  complete audit trail.
## Reproducible dependency installs

`uv.lock` pins resolved package artifacts and integrity hashes for supported platforms.
Production and CI installs must refuse lock drift:

```bash
uv sync --locked --extra ai
```

After intentionally changing `pyproject.toml`, run `uv lock`, review both files, run the
test/security suite, and commit them together. The convenience installers remain available
for first-time desktop users, but release verification always uses the locked environment.

`./install-trading-agent.sh --no-setup` (or PowerShell `-NoSetup`) performs a clean locked
installation without opening the wizard and is the mode exercised across Linux, macOS, and
Windows CI.
