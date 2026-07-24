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
