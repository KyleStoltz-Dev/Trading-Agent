# Security model

This project is decision support, not an autonomous trading system. Its most important
boundary is structural: broker connectors implement reads only. They contain no order
placement, modification, cancellation, closing, or hedging methods.

## Trust boundaries

- PostgreSQL contains journal, normalized broker executions, account snapshots, evidence
  metadata, decisions, and audit hashes. It does not contain every tick.
- Broker and model credentials are loaded from environment variables into masked secret
  types. The database stores only a configuration reference such as
  `env:OANDA_API_TOKEN`.
- Chart images are stored outside PostgreSQL under content-addressed names. Directories
  are mode `0700` and files are mode `0600`.
- Model output is untrusted. It cannot directly mutate data: runtime policy metadata and
  an explicit trader confirmation are required before a mutating tool is executed.
- A software-change request is reiterated and confirmed before a coding backend starts.
  Market, news, chart, and journal content cannot directly invoke the coding backend.
- Development runs use a separate Git worktree and a minimal environment allowlist. Model,
  broker, news, API-server, and database credentials are removed from the child process.
- The local HTTP API requires a key of at least 32 characters on every `/api/*` route.
  Mutating routes additionally require `X-Trader-Confirmation: confirmed`.

## Never store or send

- Never commit `.env`, `.env.save`, broker tokens, model API keys, or database dumps.
- Never paste credentials into an agent conversation. Recent conversation context can be
  sent to the selected model provider.
- Never pass credentials as CLI arguments. They may appear in shell history or process
  listings.
- Never treat chart labels or model interpretations as verified market facts.

## Operational controls

1. Use an OANDA practice account first. Confirm `OANDA_ENVIRONMENT=practice`.
2. Use a dedicated database role and database. Do not use a PostgreSQL superuser.
3. Keep the API bound to loopback unless authentication, TLS, and network access controls
   have been deliberately deployed.
4. Rotate any key that appears in Git history, terminal output, screenshots, logs, or
   conversations.
5. Back up before schema adoption or upgrades and keep database dumps outside the repo.
6. Run `trading-agent health --strict` before a session and after configuration changes.
7. Review reconciliation issues before relying on journal position state.
8. Review `trading-agent develop diff SESSION_ID` before locally committing a generated
   change.

## Automated checks

CI runs Ruff, security-focused Ruff rules, PostgreSQL migrations, the full test suite,
wheel-content verification, and `pip-audit`. Dependabot monitors Python and GitHub Actions
dependencies. These checks reduce risk; they do not replace credential rotation, least
privilege, or human review.

## Future execution boundary

`order_intents` and `approvals` are an audit model for previewing a possible future
workflow. Approval records do not execute anything. If broker execution is ever added, it
must live in a separate adapter/process with broker-side limits, idempotency, expiry,
fresh-price checks, account verification, a kill switch, and a second explicit trader
confirmation. Autonomous execution remains out of scope.

## Development boundary

Development mode is separate from the trading model's tool surface. The trading model
cannot call it. Only the CLI's conservative software-intent detector or the explicit
`develop` command can offer the handoff, and both require human confirmation.

The Codex subprocess receives only basic operating-system variables plus the location of
its own saved authentication. It is instructed not to read `.env`, access PostgreSQL, use
broker credentials, commit, push, or open a pull request. Repository `AGENTS.md` and the
runtime policy still prohibit autonomous broker execution. Validation is rerun before a
local approval commit.
