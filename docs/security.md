# Security model

This project is decision support, not an autonomous trading system. Its most important
boundary is structural: broker connectors implement reads only. They contain no order
placement, modification, cancellation, closing, or hedging methods.

## Trust boundaries

- PostgreSQL contains journal, normalized broker executions, account snapshots, evidence
  metadata, decisions, and audit hashes. It does not contain every tick.
- Every account-owned service operation requires an explicit immutable
  `(workspace_id, account_id)` scope. Reads filter both values, writes persist both values,
  and composite foreign keys reject cross-account/cross-workspace relationships.
- Broker credentials are resolved per trading account. Local installations store them in
  the operating-system credential vault through `keyring`; hosted installations must inject
  an external secret backend implementing `app.services.secrets.SecretBackend`. PostgreSQL
  stores only an opaque reference and append-only lifecycle metadata, never plaintext.
  Process-global broker environment variables work only when
  `BROKER_SECRET_BACKEND=legacy-env` is explicitly selected in local single-user mode.
- Dotenv discovery never reads the current working directory and never merges sources. One
  explicit, user, or install config file is selected. POSIX config files must be current-user
  owned, non-symlink regular files with mode `0600`.
- Remote PostgreSQL URLs require encrypted transport with `sslmode=require`, `verify-ca`, or
  `verify-full`; production requires `verify-full` so the certificate and hostname are both
  authenticated.
- The MetaTrader client calls six fixed GET routes, bounds response bytes, refuses redirects,
  requires bearer authentication, and requires HTTPS for a remote bridge unless the operator
  explicitly opts into trusted-network HTTP. The included MT5 service has no order route,
  disables API documentation, binds to loopback by default, and requires both an explicit
  network opt-in and direct TLS before binding to a network interface.
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
- Development runs use a separate Git worktree plus redirected ephemeral `HOME`, XDG,
  `CODEX_HOME`, and temporary directories outside the repository. The launcher deliberately
  copies Codex authentication there with mode `0600` and removes it after the run. Known
  model, broker, news, API-server, and database environment values are not inherited. This
  reduces accidental exposure but is not a read or container boundary; host-accessible files
  and staged Codex authentication may still be readable by Codex or child tools.
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
  Normal API routes additionally require `X-Workspace-ID` and `X-Account-ID`; both IDs must
  identify one real account relationship.
  A mutating client must first request a short-lived confirmation token bound to the exact
  HTTP method, path, SHA-256 of the request body, workspace, and account. The token is sent
  once in `X-Trader-Confirmation`; replay, body/path substitution, or reuse under another
  account fails. Strategy journal routes additionally require `X-Strategy-Version` for one
  immutable version owned by the selected workspace.

Hosted mode accepts high-entropy bearer principals whose SHA-256 token digests are mapped to
one exact workspace/account grant. The middleware binds that scope to transaction-local
PostgreSQL settings; RLS policies reapply those settings after every commit or rollback.
Startup refuses hosted mode unless principal authentication, RLS, a production external
secret backend, and a non-owner/non-superuser/non-`BYPASSRLS` runtime role are all present.
It also refuses a runtime role that can mutate the bootstrap `api_principals` or
`api_principal_grants` tables.
`DATABASE_AUTO_MIGRATE` must be false in hosted mode; schema upgrades run separately with
the migration-owner role before the least-privilege runtime starts.

The two principal bootstrap tables are intentionally outside tenant RLS so authentication
can occur before a tenant is known. They contain token digests and grants, not bearer tokens,
and no API endpoint exposes them. A production deployment must grant the runtime role SELECT
only on those tables; provisioning must use a separate administrative role or reviewed
security-definer procedure. Table owners bypass ordinary RLS, so migrations and runtime must
use different roles. Hosted TradingView ingestion remains disabled: its current third-party
delivery cannot establish an authenticated workspace principal before tenant RLS.
Provisioning supports `trade principal create`, `grant`, `rotate-token`, and `revoke`.
Bearer-token rotation invalidates the old token globally and records an event for every
active account grant; revocation affects only the currently selected account.

## Never store or send

- Never commit `.env`, `.env.save`, broker tokens, model API keys, or database dumps.
- Never paste credentials into an agent conversation. Recent conversation context can be
  sent to the selected model provider.
- Never pass credentials as CLI arguments. They may appear in shell history or process
  listings.
- Never treat chart labels or model interpretations as verified market facts.
- Curriculum content and lesson progress are not execution rules. Cross-framework education must
  be labeled education-only and cannot bypass the exact active-strategy boundary.

## Operational controls

1. Use an OANDA practice account first. Confirm `OANDA_ENVIRONMENT=practice`.
2. Use a dedicated database role and database. Do not use a PostgreSQL superuser.
3. Keep the API bound to loopback unless authentication, TLS, and network access controls
   have been deliberately deployed. The CLI refuses non-loopback binding without TLS files.
4. Rotate any key that appears in Git history, terminal output, screenshots, logs, or
   conversations.
5. Back up before schema adoption or upgrades and keep database dumps outside the repo.
6. Run `trading-agent health --strict` before a session and after configuration changes.
7. Review reconciliation issues before relying on journal position state.
8. Review `trading-agent develop diff SESSION_ID` before locally committing a generated
   change. The runner rejects known broker-order SDK methods, write endpoints,
   credential-shaped additions, sensitive paths, binaries, and symlinks, but these scans do
   not replace human review.
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
14. Keep the MetaTrader bridge on loopback or behind private authenticated HTTPS. Never expose
    it directly to the public internet, never reuse the broker password as its bearer token,
    and verify `METATRADER_ACCOUNT_ID` before every registration.
15. Run `trade account list` before importing or evaluating data. Use
    `trade account use ACCOUNT` to select the intended account, then restart `trade`; existing
    sessions remain bound to the account under which they were created.
16. Rotate local credentials with `trade broker credential-rotate --provider PROVIDER`.
    Tokens are entered through a hidden prompt, never a command-line argument. Removal
    disables the connection first. If vault cleanup fails, the reference is retained and a
    retry-required security audit event records the partial success. Retry that exact event
    with `trade broker credential-cleanup-retry EVENT_UUID`; the service refuses to delete a
    reference still used by an active connection.

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
- The host fixes workspace/account scope before querying. Untrusted alert, note, chart, or
  model text cannot switch accounts, and database relationships cannot attach retrieved data
  to a different account.
- Web fetching is HTTPS GET-only and constrained by public-IP pinning, explicit domain and path
  allowlists, exact per-request and per-redirect confirmation, redirect revalidation, size
  limits, and bans on model-selected query parameters and secret-like path content.
- Interactive chart paths must appear exactly in the current user request, resolve under
  `CHART_ALLOWED_ROOTS` or the evidence directory, and be regular non-symlink files. Hosted
  analysis requires confirmation of the provider, destination, path, byte count, and context;
  loopback Ollama analysis remains local. `CHART_ALLOWED_ROOTS` is empty by default; add only
  a narrow screenshot/staging directory, not a home directory or filesystem root.
- `trade chart --clipboard` asks the operating system only for PNG, JPEG, or WebP image
  representations, verifies their file signatures, and enforces the same 10 MB limit. It never
  reads clipboard text as a path or command. Temporary clipboard staging is private and removed
  before the command continues. Hosted analysis shows the clipboard source, byte count, MIME
  type, context, provider, and destination for confirmation before disclosure.
- The model has no arbitrary SQL, shell, filesystem, credential, or broker-execution tool.
- Trader-profile inputs use bounded, field-specific validation at both the interactive wizard
  and Pydantic schema boundaries. Control/bidirectional characters, role delimiters,
  credential-shaped values, URLs, and paired model-control instructions are rejected. Goals
  must be trading/process relevant; reflective trading-style prose remains distinct from short
  labels. Suspected secrets are not repeated in validation errors.
- Stored profile text, including legacy rows created before these validators, is returned to the
  model only inside an explicit untrusted-content envelope. Terminal review cells use literal
  text rendering so Rich markup in stored/user text cannot spoof review styling.
- Startup recall is rendered locally by default and is never silently disclosed to a model.
  `/memory use` requires per-request confirmation naming the active provider. The disclosed
  envelope is bounded, source-referenced, exact-strategy-scoped, and explicitly untrusted; raw
  old conversation turns, reflection notes, mindset notes, and free-form emotional prose are
  excluded.
- Personal/prop account constraints contain no broker credentials. Firm/program names and
  custom restrictions are bounded untrusted text. The host calculates percentage-to-amount
  reminders deterministically, identifies unknown rules, and labels compliance as unverified
  unless fresh broker and firm-side state supports it.
- TradingView webhook ingestion is disabled by default. In production, the app accepts proxy
  verification headers only from configured trusted proxy CIDRs, requires TradingView's
  certificate identity and official source IP, bounds the body before JSON parsing, validates
  a strict schema, resolves the route's account inside the configured workspace, and
  deduplicates delivery. The public proxy must strip client-supplied verification headers
  before setting its own. Alert values remain untrusted evidence and cannot select a strategy
  or call a broker.

These controls limit impact; they do not make untrusted content truthful. A trader can still
approve a harmful journal mutation or private search, an allowlisted site can be compromised,
and information intentionally sent to a remote model provider is subject to that provider's
data handling. Keep secrets out of prompts and journal text, use least-privilege credentials,
and verify sources and confirmation panels.

Local Ollama is limited to loopback by default. A non-local `OLLAMA_BASE_URL` is rejected unless
`OLLAMA_ALLOW_REMOTE=true` is explicitly configured, because remote inference sends prompts,
chart images, conversation context, and requested tool results to that host. Credentials,
queries, and fragments are not accepted in the Ollama base URL. Remote Ollama must use HTTPS
unless `OLLAMA_ALLOW_INSECURE_REMOTE=true` is deliberately enabled for a protected private
network. Production Ollama requires exact configured SHA-256 model digests.

Provider calls share a bounded process-wide concurrency limiter and queue timeout. Only a
bounded number of recent completed turns is sent as model context. Database journal history
remains an audit record until the operator applies an explicit retention policy; hosted
providers remain subject to their own retention and abuse-monitoring terms.

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

The Codex subprocess receives a reduced environment plus an ephemeral copy of its own saved
authentication. Its home, XDG, Codex, Git-global-config, and temporary paths are redirected
away from the normal user paths. This is not a confidentiality boundary. Codex
`workspace-write` limits writes but does not provide a filesystem-read or container boundary;
Codex and child tools may read any host path permitted to the process, including its staged
Codex authentication. Environment filtering also cannot prove that no secret is reachable.

Development therefore defaults off, is rejected outside `APP_ENV=development`, and requires
the explicit
`DEVELOPMENT_ACKNOWLEDGE_HOST_FILESYSTEM_READ_RISK=true` acknowledgment. Enable it only for
a trusted local machine, repository, and request. A repository that tracks `.env` or
`.env.save` is rejected before the subprocess starts. Codex is instructed not to access
PostgreSQL, use broker credentials, commit, push, or open a pull request. Repository
`AGENTS.md` and runtime policy still prohibit autonomous broker execution. Added diff lines
are scanned for known broker-order SDK calls, write endpoints, credential-shaped values,
sensitive paths, binaries, and symlinks. These reduce risk but do not create containment.

The API confirmation challenge is replay protection and request-intent binding, not a second
identity factor. Anyone holding the API key can request a challenge, so keep the API on
loopback unless TLS and network access controls have been deliberately deployed. The built-in
server refuses non-loopback binding unless both TLS files are supplied.
