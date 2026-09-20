# Conversation and settings stabilization

This change is stacked on the scoped account-state fixes in PR #54. It does not
include the separate model-selection work in PR #53 or unrelated local Discord,
news, TradingView bundle import, backtesting, or dashboard-confirmation features.

## What changes

- Scoped, versioned workflow intent is saved with each user turn. Restarting or
  exhausting the model history window does not lose the current instrument/stage.
- Terminal and gateway use the same policy-checked context assembly. A request uses
  its own checkpoint, even if another interface subsequently changes instruments.
- Cancellation/failure accounting is shared. Pending work is rolled back; already
  committed mutations remain audited and are not claimed to have been undone.
- Settings snapshot, dependent operation and rollback share one recursive file
  lock. Another interface's successful write cannot be erased by stale rollback.
- Agent instructions and tool declarations are separated from runtime execution.
  No additional tools or execution permissions are introduced.

## Database rollout

Migration `c41e8b6d920a` adds nullable JSONB `workflow_checkpoint` to conversation
turns. Existing tenant controls remain in effect, and no backfill is required.
Legacy intent is bootstrapped from bounded, strategy-scoped user history.

Close running sessions before migrating. A five-second lock timeout prevents an
indefinite wait on open transactions; do not terminate unrelated database clients.
This is an additive migration: saved trades, settings and journal records are not
deleted. The developer's local database has already been upgraded and checked.

## Verification and limits

September 20, 2026: **795 tests passed**, with no skips and one existing Starlette
TestClient deprecation warning. Ruff quality/security checks and whitespace checks
passed. Alembic reported no schema differences before and after a successful
downgrade/upgrade drill against the disposable database.

Branch verification uses an isolated PostgreSQL database and controlled provider
responses, not private trading records or live model/broker calls. Regression
coverage includes all workflow stages, restart, account/strategy isolation,
concurrent intent, provider failure, cancellation and settings-writer races.

Workflow checkpoints record intent, not orders, eligibility or completed domain
actions. Broker integrations remain read-only and confirmation requirements are
unchanged. Settings/database coordination is not crash-atomic across resources.
The CLI still needs further incremental decomposition, and real voice/terminal
interaction needs user acceptance testing.
