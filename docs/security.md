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
- Imported social/file content is untrusted data. It is size bounded, content hashed,
  deduplicated, archive-path checked, and tied to one exact immutable strategy version.
- The model can query only bounded application-owned database tools. It has no arbitrary
  SQL tool, and strategy knowledge/test queries fail closed across version boundaries.
- Model output is untrusted. It cannot directly mutate data: runtime policy metadata and
  an explicit trader confirmation are required before a mutating tool is executed.
- A software-change request is reiterated and confirmed before a coding backend starts.
  Market, news, chart, and journal content cannot directly invoke the coding backend.
- Development runs use a separate Git worktree plus ephemeral `HOME`, XDG, `CODEX_HOME`, and
  temporary directories outside the repository. Only Codex authentication is copied into the
  isolated runtime, with mode `0600`, and that copy is removed after the run. Model, broker,
  news, API-server, database, and arbitrary parent-environment values are not inherited.
- Guided setup rewrites only an allowlist of non-secret configuration keys, uses an atomic
  replacement, and enforces mode `0600`. It refuses API-key fields.
- Harness files are application-owned, size-bounded context. They do not define executable
  tools and cannot override runtime policy or deterministic risk controls.
- Full-page web reads are GET-only, size/time bounded, content-type limited, restricted to
  configured domains and public IP addresses, and every redirect is revalidated.
- Broad web search is disabled by default. When enabled, its key is a masked secret and
  search results remain untrusted snippets; a result cannot expand the fetch allowlist.
  Before any tier-3 request leaves the machine, the host validates the query and requires
  the trader to approve a disclosure panel showing the exact normalized query, provider,
  destination, and escalation reason. Declining makes no network request.
- Imported knowledge, provider news/calendar text, fetched pages, and search snippets are
  returned to the model inside explicit untrusted-content envelopes with source type and
  provenance. Text inside those envelopes cannot change tool metadata, runtime policy,
  active strategy scope, or the confirmation requirement.
- Reference labels and locators are rendered as terminal text, preventing fetched titles
  from being interpreted as Rich terminal markup.
- The local HTTP API requires a key of at least 32 characters on every `/api/*` route.
  A mutating client must first request a short-lived confirmation token bound to the exact
  HTTP method, path, and SHA-256 of the request body. The token is sent once in
  `X-Trader-Confirmation`; replay or body/path substitution fails. Strategy journal routes
  additionally require `X-Strategy-Version` for one immutable version.

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
   change. The runner also rejects generated diffs containing known broker-order SDK methods
   or write endpoints, but this scan does not replace human review.
9. Use `/context` when auditing which harness material influenced a response.
10. Use `/sources` after market, news, execution, or strategy analysis and verify that
    time-sensitive claims point to the expected broker/provider URL and timestamp.
11. Keep `WEB_SEARCH_PROVIDER=disabled` unless broader discovery is needed. Add domains and
    narrow documentation prefixes to `WEB_FETCH_ALLOWED_DOMAINS` and
    `WEB_FETCH_ALLOWED_PATHS` deliberately rather than allowing arbitrary full-page fetch.
12. Prefer official Discord, Telegram, and X exports. Treat third-party exporters as
    untrusted software and never paste a personal user token into the agent or repository.
13. Read every tier-3 disclosure panel. A query can reveal a private trading idea even when
    it contains no recognizable password or token, so pattern checks do not replace review.

## Prompt-injection boundary

The application does not claim that prompts can sanitize hostile text. A webpage, chart label,
Discord export, Telegram message, X archive, journal note, or search snippet may tell the model
to ignore policy, retrieve another strategy, disclose data, mutate the journal, or invoke a
tool. The model may still misunderstand or repeat that instruction.

Security therefore depends on host-enforced capabilities:

- The tool surface is fixed and checked against runtime policy at startup.
- Journal mutations are blocked until the trader confirms the exact proposed arguments.
- Tier-3 searches are blocked until the trader confirms the exact outbound query.
- Strategy retrieval takes the active immutable version from host state, not from model
  arguments, and conversation history is filtered by that same version.
- Web fetching is HTTPS GET-only and constrained by public-IP pinning, explicit domain and path
  allowlists, exact per-request and per-redirect confirmation, redirect revalidation, size
  limits, and bans on model-selected query parameters and secret-like path content.
- Interactive chart paths must appear exactly in the current user request, resolve under
  `CHART_ALLOWED_ROOTS` or the evidence directory, and be regular non-symlink files. Hosted
  analysis requires confirmation of the provider, destination, path, byte count, and context;
  loopback Ollama analysis remains local. `CHART_ALLOWED_ROOTS` is empty by default; add only
  a narrow screenshot/staging directory, not a home directory or filesystem root.
- The model has no arbitrary SQL, shell, filesystem, credential, or broker-execution tool.

These controls limit impact; they do not make untrusted content truthful. A trader can still
approve a harmful journal mutation or private search, an allowlisted site can be compromised,
and information intentionally sent to a remote model provider is subject to that provider's
data handling. Keep secrets out of prompts and journal text, use least-privilege credentials,
and verify sources and confirmation panels.

Local Ollama is limited to loopback by default. A non-local `OLLAMA_BASE_URL` is rejected unless
`OLLAMA_ALLOW_REMOTE=true` is explicitly configured, because remote inference sends prompts,
chart images, conversation context, and requested tool results to that host. Credentials,
queries, and fragments are not accepted in the Ollama base URL.

The web fetcher blocks loopback, private, link-local, multicast, and other non-public resolved
addresses. The documented-domain allowlist is the primary trust boundary; do not add domains
that can host user-controlled redirects or arbitrary uploaded content without reviewing that
risk.

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

The Codex subprocess receives only basic operating-system variables plus an ephemeral copy
of its own saved authentication. Its home, XDG, Codex, Git-global-config, and temporary paths
are isolated from the user's home and repository. A repository that tracks `.env` or
`.env.save` is rejected before the subprocess starts. It is instructed not to access
PostgreSQL, use broker credentials, commit, push, or open a pull request. Repository
`AGENTS.md` and the runtime policy still prohibit autonomous broker execution. Added diff
lines are scanned for known broker-order SDK calls and write endpoints, and validation plus
that scan are rerun before a local approval commit.

The API confirmation challenge is replay protection and request-intent binding, not a second
identity factor. Anyone holding the API key can request a challenge, so keep the API on
loopback unless TLS and network access controls have been deliberately deployed.
