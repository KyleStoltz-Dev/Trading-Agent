# Incremental codebase health work

## Completed in the first cleanup

- Reconciled the stabilization merge with main's model persistence and CSV bundle
  work. PR #55 had been merged into the account branch after PR #54 reached main;
  this integration brings that missing work forward without reverting #51 or #53.
- Extracted 17 command implementations into `app/terminal/commands`: news,
  knowledge, experiments and saved sessions. Command registration, public names,
  argument validation, defaults and help remain in `app/cli.py` as thin facades.
- Extracted pure markdown normalization into `app/terminal/formatting.py`.
- Added an immutable, per-invocation `CommandRuntime` for console, database,
  settings, scope resolution and existing policy/audit callbacks. Command modules
  do not import the CLI back, replace policy checks, or create global UI state.
- Added command signature/forwarding/help contracts and regression tests for
  denied mutations, scoped transcripts, provider failures and separate consoles.
- Added import-boundary tests: shared services/connectors must not import terminal
  or API entrypoints, and must not import optional model SDKs directly. Formatting
  remains independent of database, provider and application state.

The reconciled CLI was 11,712 lines before this extraction and is now 10,995.
This is a first slice, not a claim that the CLI or application is fully decomposed.
No commands, tools, order permissions or database changes are added by the CLI
extraction itself. The integration includes the previously tested additive
workflow-checkpoint migration from #55.

## Next reviewable slices

1. Extract journal, broker and setup command implementations, then split the chat
   loop into input handling, command routing and request execution. Preserve input
   cancellation, credentials rollback, audit hooks and exact account scope.
2. Split tool execution by capability behind the existing policy-wrapped executor.
   Keep one authoritative catalog, confirmation boundary and evidence ledger.
3. Split HTTP routes from authentication, confirmation and app initialization;
   preserve middleware order and route dependency requirements with contract tests.
4. Separate dashboard styles, API client and interaction modules without requiring
   a new UI framework. Keep secrets out of browser storage and preserve escaping.
5. Introduce incremental type checking and coverage reporting for shared contracts;
   expand end-to-end interface journeys and real-provider acceptance testing.

Avoid cosmetic moves of the entire models/schema files before these operational
boundaries are clearer. Keep each follow-up behavior-preserving where possible,
and isolate actual bug fixes from mechanical moves in reviewable commits.

## Verification

September 20, 2026: the reconciled baseline passed 811 tests. With the extraction
and new boundary tests, **843 passed**, none skipped (one existing Starlette
TestClient deprecation warning). Ruff quality/security checks, schema consistency,
the migration round-trip, wheel/source archive checks and inclusion of all new
terminal modules passed. AST comparison confirmed that the 18 extracted command
implementations/helpers retain their original logic after dependency substitution.

Use the exact branch with isolated PostgreSQL and controlled model/connector
responses. Run the full suite, Ruff, security lint, package checks and migration
checks before merging. Test counts describe only this branch, not unrelated local
research work. Do not infer live broker reachability or voice quality from mocks.
