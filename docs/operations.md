# Operations

## Startup

Normal daily startup:

```bash
trade
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

Or, after installation:

```bash
trade setup
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
application. For PostgreSQL, `docker compose up -d postgres` is the most consistent option
across operating systems, while native installations remain supported.

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

When Trading Economics is selected and configured, startup refreshes the upcoming calendar.
Trade-intent requests receive a nearby-event warning and timestamped event context.

## Configuration discovery

Configuration lookup order is:

1. `TRADING_AGENT_CONFIG` when explicitly set;
2. `.env` beside an editable source installation;
3. `~/.config/trading-agent/.env`;
4. `.env` in the current directory.

Later files override earlier files. Environment variables override all files. `trade setup`
updates the active file atomically with mode `0600` and refuses to write secret keys.

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
  --emotion focused --emotion patient --trade xauusd-20260725-ny-short-1 \
  --note "The predefined loss is acceptable."
trade mindset list --limit 10
```

Valid phases are `pre-session`, `pre-trade`, `during-trade`, and `post-trade` in the CLI
(Typer also accepts their underscore forms). A check-in may reference a trade by its readable
reference or internal UUID. Agent-created check-ins require explicit confirmation; recent
check-in retrieval is read-only.

`/sources` shows the full reference ledger for the previous answer. It always includes the
runtime policy and selected harness resources, then adds journal records, charts, broker
observations, news/calendar items, fetched pages, and search results actually used.

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

In interactive chat, state a clear software change or use `/develop`. After the scope
confirmation, the command may run for several minutes. It records a local session under
`.data/development`, creates an `agent/dev-*` branch in a separate worktree, runs Codex
non-interactively with workspace-only writes, then runs Ruff and pytest.

Inspect and commit the result:

```bash
trading-agent develop status SESSION_ID
trading-agent develop diff SESSION_ID
trading-agent develop approve SESSION_ID --yes
```

Approval reruns validation and commits only to the isolated branch. Push, pull-request,
merge, deployment, and process restart are deliberately separate operations.

`DEVELOPMENT_APPROVAL_FLOW=scope_only` removes the second commit approval: the initial
reiterated-scope confirmation authorizes Codex to edit, validate, and commit on the isolated
branch. `scope_and_diff` is the default for shared installations.

Codex runs with ephemeral home, XDG, Codex, Git-config, and temporary directories outside
the repository. Only its authentication file is copied into that runtime, and the copy is
removed afterward. Tracked `.env`/`.env.save` files stop the handoff. A host-side diff scan
also rejects known broker-order SDK calls and write endpoints before review or approval.

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
fill ledger. A mismatch marks the connection degraded.

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

Set `TRADING_ECONOMICS_API_KEY`, then:

```bash
trading-agent news sync --help
```

Only provider metadata and summaries are retained: external ID, original timestamps,
retrieval timestamp, importance, country/category/symbol, values, and source URL. Provider
entitlements and redistribution terms still apply.

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
trading-agent edge report --minimum-sample 30
```

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
- Test restores periodically with `pg_restore` into a separate database.
- If reconciliation becomes degraded, stop relying on derived position state, verify the
  configured account and cursor, and compare broker transactions against imported fills.
- Evidence is file-backed. Back up both PostgreSQL and `.data/evidence` to preserve a
  complete audit trail.
