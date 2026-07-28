"""scope workspaces, accounts, decisions, memory, and broker ingestion

Revision ID: a73f1c9d4e20
Revises: f62b9d4e1a30
Create Date: 2026-07-27 03:15:00.000000

The migration is intentionally staged:

1. Expand with nullable scope columns.
2. Create deterministic legacy identities and propagate known account ownership.
3. Reject ambiguous or mismatched relationships.
4. Enforce NOT NULL, scoped uniqueness, and composite foreign keys.

No server default is retained for a workspace or account identity. New writes must
therefore choose their scope explicitly.
"""

# ruff: noqa: S608 - dynamic identifiers come only from fixed module allowlists.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a73f1c9d4e20"
down_revision: str | Sequence[str] | None = "f62b9d4e1a30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_UNASSIGNED_ACCOUNT_ID = "00000000-0000-4000-8000-000000000002"

WORKSPACE_ONLY_TABLES = (
    "playbooks",
    "playbook_versions",
    "knowledge_imports",
    "strategy_knowledge_items",
)

ACCOUNT_SCOPE_COLUMNS = {
    "broker_connections": ("workspace_id",),
    "connector_cursors": ("workspace_id", "account_id"),
    "trader_profiles": ("workspace_id", "account_id"),
    "account_constraint_profiles": ("workspace_id", "trading_account_id"),
    "strategy_experiments": ("workspace_id", "account_id"),
    "strategy_test_samples": ("workspace_id", "account_id"),
    "trades": ("workspace_id",),
    "trade_plans": ("workspace_id",),
    "order_intents": ("workspace_id", "account_id"),
    "order_approvals": ("workspace_id", "account_id"),
    "execution_events": ("workspace_id", "account_id"),
    "fills": ("workspace_id", "account_id"),
    "trade_management_events": ("workspace_id", "account_id"),
    "position_snapshots": ("workspace_id",),
    "account_snapshots": ("workspace_id",),
    "market_contexts": ("workspace_id", "account_id"),
    "tradingview_alerts": ("workspace_id", "account_id"),
    "evidence_items": ("workspace_id", "account_id"),
    "analysis_runs": ("workspace_id", "account_id"),
    "observations": ("workspace_id", "account_id"),
    "trade_reflections": ("workspace_id", "account_id"),
    "rule_evaluations": ("workspace_id", "account_id"),
    "mindset_checkins": ("workspace_id", "account_id"),
    "pretrade_assessments": ("workspace_id", "account_id"),
    "conversation_sessions": ("workspace_id", "account_id"),
    "conversation_turns": ("workspace_id", "account_id"),
}

WORKSPACE_FK_TABLES = (
    "trading_accounts",
    *WORKSPACE_ONLY_TABLES,
    *ACCOUNT_SCOPE_COLUMNS,
)


def _uuid_column(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.UUID(), nullable=nullable)


def _assert_no_rows(query: str, message: str) -> None:
    escaped = message.replace("'", "''")
    op.execute(
        sa.text(
            f"""
            DO $migration$
            BEGIN
                IF EXISTS ({query}) THEN
                    RAISE EXCEPTION '{escaped}';
                END IF;
            END
            $migration$;
            """
        )
    )


def _add_scope_columns() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("slug <> ''", name="ck_workspace_slug_not_empty"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_workspace_slug"),
    )
    op.create_index("ix_workspaces_active", "workspaces", ["active"])
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"])

    op.add_column("trading_accounts", _uuid_column("workspace_id"))
    op.add_column("trading_accounts", sa.Column("is_default", sa.Boolean(), nullable=True))
    op.add_column("instrument_specifications", _uuid_column("workspace_id"))

    for table in WORKSPACE_ONLY_TABLES:
        op.add_column(table, _uuid_column("workspace_id"))
    for table, columns in ACCOUNT_SCOPE_COLUMNS.items():
        for column in columns:
            op.add_column(table, _uuid_column(column))


def _backfill_legacy_scope() -> None:
    op.execute(
        sa.text(
            f"""
            INSERT INTO workspaces (id, slug, name, active)
            VALUES (
                '{LEGACY_WORKSPACE_ID}'::uuid,
                'legacy-local',
                'Legacy local workspace',
                TRUE
            );

            UPDATE trading_accounts
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                is_default = FALSE;

            INSERT INTO trading_accounts (
                id, workspace_id, broker, external_account_id, label,
                currency, mode, active, is_default
            )
            VALUES (
                '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid,
                '{LEGACY_WORKSPACE_ID}'::uuid,
                'manual',
                'legacy-unassigned',
                'Legacy / unassigned',
                'USD',
                'practice',
                FALSE,
                FALSE
            );

            -- Each legacy constraint represented a manually described account.
            -- Preserve that distinction instead of collapsing all constraints into
            -- the catch-all account. md5(text)::uuid is deterministic and requires
            -- no PostgreSQL extension.
            CREATE TEMP TABLE legacy_constraint_scope_map ON COMMIT DROP AS
            SELECT
                acp.id AS constraint_id,
                acp.profile_id AS original_profile_id,
                md5('legacy-constraint-account:' || acp.id::text)::uuid AS account_id,
                row_number() OVER (
                    PARTITION BY acp.profile_id
                    ORDER BY acp.active DESC, acp.created_at, acp.id
                ) AS profile_rank
            FROM account_constraint_profiles AS acp;

            INSERT INTO trading_accounts (
                id, workspace_id, broker, external_account_id, label,
                currency, mode, active, is_default
            )
            SELECT
                scope.account_id,
                '{LEGACY_WORKSPACE_ID}'::uuid,
                'manual',
                'legacy-constraint:' || acp.id::text,
                acp.name,
                acp.currency,
                'practice',
                acp.active,
                FALSE
            FROM account_constraint_profiles AS acp
            JOIN legacy_constraint_scope_map AS scope
              ON scope.constraint_id = acp.id;

            -- The first constraint keeps the original trader-profile identity.
            UPDATE trader_profiles AS profile
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = scope.account_id
            FROM legacy_constraint_scope_map AS scope
            WHERE scope.original_profile_id = profile.id
              AND scope.profile_rank = 1;

            -- A legacy profile could own several constraint records. The new model
            -- makes preferences account-specific, so duplicate the profile for every
            -- additional manually described account rather than entangling accounts.
            INSERT INTO trader_profiles (
                id, workspace_id, account_id, profile_key, display_name, timezone,
                experience_level, trading_style, markets, sessions, goals,
                risk_preferences, onboarding_complete, created_at, updated_at
            )
            SELECT
                md5('legacy-constraint-profile:' || scope.constraint_id::text)::uuid,
                '{LEGACY_WORKSPACE_ID}'::uuid,
                scope.account_id,
                original.profile_key,
                original.display_name,
                original.timezone,
                original.experience_level,
                original.trading_style,
                original.markets,
                original.sessions,
                original.goals,
                original.risk_preferences,
                original.onboarding_complete,
                original.created_at,
                original.updated_at
            FROM legacy_constraint_scope_map AS scope
            JOIN trader_profiles AS original
              ON original.id = scope.original_profile_id
            WHERE scope.profile_rank > 1;

            UPDATE trader_profiles
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE workspace_id IS NULL;

            UPDATE account_constraint_profiles AS acp
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                trading_account_id = scope.account_id,
                profile_id = CASE
                    WHEN scope.profile_rank = 1 THEN scope.original_profile_id
                    ELSE md5(
                        'legacy-constraint-profile:' || scope.constraint_id::text
                    )::uuid
                END
            FROM legacy_constraint_scope_map AS scope
            WHERE scope.constraint_id = acp.id;

            UPDATE playbooks
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid;
            -- This is schema ownership metadata, not a strategy-content edit.
            -- Temporarily suspend the row-mutation trigger only for the backfill.
            ALTER TABLE playbook_versions
            DISABLE TRIGGER trg_playbook_versions_immutable;
            UPDATE playbook_versions
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid;
            ALTER TABLE playbook_versions
            ENABLE TRIGGER trg_playbook_versions_immutable;
            UPDATE knowledge_imports
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid;
            UPDATE strategy_knowledge_items
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid;

            UPDATE broker_connections AS connection
            SET workspace_id = account.workspace_id
            FROM trading_accounts AS account
            WHERE account.id = connection.account_id;

            UPDATE connector_cursors AS cursor
            SET workspace_id = connection.workspace_id,
                account_id = connection.account_id
            FROM broker_connections AS connection
            WHERE connection.id = cursor.connection_id;

            UPDATE instrument_specifications AS specification
            SET workspace_id = account.workspace_id
            FROM trading_accounts AS account
            WHERE account.id = specification.account_id;

            UPDATE trades AS trade
            SET workspace_id = account.workspace_id
            FROM trading_accounts AS account
            WHERE account.id = trade.account_id;

            -- Preserve every known plan account. Only plans with no account use a
            -- related trade account; truly unscoped plans use Legacy / unassigned.
            UPDATE trade_plans AS plan
            SET account_id = trade.account_id
            FROM trades AS trade
            WHERE plan.account_id IS NULL
              AND plan.trade_id = trade.id;
            UPDATE trade_plans
            SET account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;
            UPDATE trade_plans AS plan
            SET workspace_id = account.workspace_id
            FROM trading_accounts AS account
            WHERE account.id = plan.account_id;

            UPDATE strategy_experiments
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid;
            UPDATE strategy_test_samples AS sample
            SET workspace_id = experiment.workspace_id,
                account_id = experiment.account_id
            FROM strategy_experiments AS experiment
            WHERE experiment.id = sample.experiment_id;

            DO $mindset_scope$
            BEGIN
                IF EXISTS (
                    SELECT assessment.mindset_checkin_id
                    FROM pretrade_assessments AS assessment
                    JOIN account_constraint_profiles AS constraint_profile
                      ON constraint_profile.id =
                         assessment.account_constraint_profile_id
                    WHERE assessment.mindset_checkin_id IS NOT NULL
                    GROUP BY assessment.mindset_checkin_id
                    HAVING count(
                        DISTINCT constraint_profile.trading_account_id
                    ) > 1
                ) THEN
                    RAISE EXCEPTION
                        'a legacy mindset check-in belongs to multiple accounts';
                END IF;
            END
            $mindset_scope$;

            UPDATE mindset_checkins AS mindset
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = mindset.trade_plan_id;
            UPDATE mindset_checkins AS mindset
            SET workspace_id = constraint_profile.workspace_id,
                account_id = constraint_profile.trading_account_id
            FROM pretrade_assessments AS assessment
            JOIN account_constraint_profiles AS constraint_profile
              ON constraint_profile.id = assessment.account_constraint_profile_id
            WHERE assessment.mindset_checkin_id = mindset.id
              AND mindset.account_id IS NULL;
            UPDATE mindset_checkins
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;

            UPDATE pretrade_assessments AS assessment
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = assessment.trade_plan_id;
            UPDATE pretrade_assessments AS assessment
            SET workspace_id = mindset.workspace_id,
                account_id = mindset.account_id
            FROM mindset_checkins AS mindset
            WHERE mindset.id = assessment.mindset_checkin_id
              AND assessment.account_id IS NULL;
            UPDATE pretrade_assessments AS assessment
            SET workspace_id = constraint_profile.workspace_id,
                account_id = constraint_profile.trading_account_id
            FROM account_constraint_profiles AS constraint_profile
            WHERE constraint_profile.id = assessment.account_constraint_profile_id
              AND assessment.account_id IS NULL;
            UPDATE pretrade_assessments
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;

            UPDATE market_contexts AS context
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = context.trade_plan_id;
            UPDATE market_contexts
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;

            UPDATE evidence_items AS evidence
            SET workspace_id = trade.workspace_id,
                account_id = trade.account_id
            FROM trades AS trade
            WHERE trade.id = evidence.trade_id;
            UPDATE evidence_items AS evidence
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = evidence.trade_plan_id
              AND evidence.account_id IS NULL;
            UPDATE evidence_items AS evidence
            SET workspace_id = context.workspace_id,
                account_id = context.account_id
            FROM market_contexts AS context
            WHERE context.id = evidence.market_context_id
              AND evidence.account_id IS NULL;
            UPDATE evidence_items
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;

            UPDATE analysis_runs AS run
            SET workspace_id = evidence.workspace_id,
                account_id = evidence.account_id
            FROM evidence_items AS evidence
            WHERE evidence.id = run.evidence_id;
            UPDATE analysis_runs AS run
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = run.trade_plan_id
              AND run.account_id IS NULL;
            UPDATE analysis_runs
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;

            UPDATE observations AS observation
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = observation.trade_plan_id;
            UPDATE observations AS observation
            SET workspace_id = context.workspace_id,
                account_id = context.account_id
            FROM market_contexts AS context
            WHERE context.id = observation.market_context_id
              AND observation.account_id IS NULL;
            UPDATE observations AS observation
            SET workspace_id = evidence.workspace_id,
                account_id = evidence.account_id
            FROM evidence_items AS evidence
            WHERE evidence.id = observation.evidence_id
              AND observation.account_id IS NULL;
            UPDATE observations
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;

            UPDATE trade_reflections AS reflection
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = reflection.trade_id;
            UPDATE rule_evaluations AS evaluation
            SET workspace_id = reflection.workspace_id,
                account_id = reflection.account_id
            FROM trade_reflections AS reflection
            WHERE reflection.id = evaluation.reflection_id;

            UPDATE order_intents AS intent
            SET workspace_id = trade.workspace_id,
                account_id = trade.account_id
            FROM trades AS trade
            WHERE trade.id = intent.trade_id;
            UPDATE order_intents AS intent
            SET workspace_id = plan.workspace_id,
                account_id = plan.account_id
            FROM trade_plans AS plan
            WHERE plan.id = intent.trade_plan_id
              AND intent.account_id IS NULL;
            UPDATE order_intents
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid
            WHERE account_id IS NULL;

            UPDATE order_approvals AS approval
            SET workspace_id = intent.workspace_id,
                account_id = intent.account_id
            FROM order_intents AS intent
            WHERE intent.id = approval.order_intent_id;

            UPDATE execution_events AS event
            SET workspace_id = connection.workspace_id,
                account_id = connection.account_id
            FROM broker_connections AS connection
            WHERE connection.id = event.connection_id;
            UPDATE fills AS fill
            SET workspace_id = connection.workspace_id,
                account_id = connection.account_id
            FROM broker_connections AS connection
            WHERE connection.id = fill.connection_id;
            UPDATE trade_management_events AS event
            SET workspace_id = trade.workspace_id,
                account_id = trade.account_id
            FROM trades AS trade
            WHERE trade.id = event.trade_id;
            UPDATE position_snapshots AS snapshot
            SET workspace_id = account.workspace_id
            FROM trading_accounts AS account
            WHERE account.id = snapshot.account_id;
            UPDATE account_snapshots AS snapshot
            SET workspace_id = account.workspace_id
            FROM trading_accounts AS account
            WHERE account.id = snapshot.account_id;

            UPDATE tradingview_alerts
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid;

            UPDATE conversation_sessions
            SET workspace_id = '{LEGACY_WORKSPACE_ID}'::uuid,
                account_id = '{LEGACY_UNASSIGNED_ACCOUNT_ID}'::uuid;
            UPDATE conversation_turns AS turn
            SET workspace_id = session.workspace_id,
                account_id = session.account_id
            FROM conversation_sessions AS session
            WHERE session.id = turn.session_id;
            """
        )
    )


def _validate_backfill() -> None:
    not_null_checks = [
        "SELECT 1 FROM trading_accounts WHERE workspace_id IS NULL OR is_default IS NULL",
        *[
            f"SELECT 1 FROM {table} WHERE workspace_id IS NULL"
            for table in WORKSPACE_ONLY_TABLES
        ],
        *[
            f"SELECT 1 FROM {table} WHERE "
            + " OR ".join(f"{column} IS NULL" for column in columns)
            for table, columns in ACCOUNT_SCOPE_COLUMNS.items()
        ],
        "SELECT 1 FROM trade_plans WHERE account_id IS NULL",
    ]
    _assert_no_rows(
        " UNION ALL ".join(not_null_checks),
        "workspace/account backfill left required scope columns null",
    )

    mismatch_checks = (
        """
        SELECT 1 FROM trade_plans p
        JOIN trades t ON t.id = p.trade_id
        WHERE (p.workspace_id, p.account_id) IS DISTINCT FROM
              (t.workspace_id, t.account_id)
        """,
        """
        SELECT 1 FROM mindset_checkins m
        JOIN trade_plans p ON p.id = m.trade_plan_id
        WHERE (m.workspace_id, m.account_id) IS DISTINCT FROM
              (p.workspace_id, p.account_id)
        """,
        """
        SELECT 1 FROM pretrade_assessments a
        JOIN mindset_checkins m ON m.id = a.mindset_checkin_id
        WHERE (a.workspace_id, a.account_id) IS DISTINCT FROM
              (m.workspace_id, m.account_id)
        """,
        """
        SELECT 1 FROM pretrade_assessments a
        JOIN account_constraint_profiles c
          ON c.id = a.account_constraint_profile_id
        WHERE (a.workspace_id, a.account_id) IS DISTINCT FROM
              (c.workspace_id, c.trading_account_id)
        """,
        """
        SELECT 1 FROM pretrade_assessments a
        JOIN trade_plans p ON p.id = a.trade_plan_id
        WHERE (a.workspace_id, a.account_id) IS DISTINCT FROM
              (p.workspace_id, p.account_id)
        """,
        """
        SELECT 1 FROM evidence_items e
        JOIN trades t ON t.id = e.trade_id
        WHERE (e.workspace_id, e.account_id) IS DISTINCT FROM
              (t.workspace_id, t.account_id)
        """,
        """
        SELECT 1 FROM evidence_items e
        JOIN trade_plans p ON p.id = e.trade_plan_id
        WHERE (e.workspace_id, e.account_id) IS DISTINCT FROM
              (p.workspace_id, p.account_id)
        """,
        """
        SELECT 1 FROM evidence_items e
        JOIN market_contexts c ON c.id = e.market_context_id
        WHERE (e.workspace_id, e.account_id) IS DISTINCT FROM
              (c.workspace_id, c.account_id)
        """,
        """
        SELECT 1 FROM analysis_runs r
        JOIN evidence_items e ON e.id = r.evidence_id
        WHERE (r.workspace_id, r.account_id) IS DISTINCT FROM
              (e.workspace_id, e.account_id)
        """,
        """
        SELECT 1 FROM analysis_runs r
        JOIN trade_plans p ON p.id = r.trade_plan_id
        WHERE (r.workspace_id, r.account_id) IS DISTINCT FROM
              (p.workspace_id, p.account_id)
        """,
        """
        SELECT 1 FROM observations o
        JOIN trade_plans p ON p.id = o.trade_plan_id
        WHERE (o.workspace_id, o.account_id) IS DISTINCT FROM
              (p.workspace_id, p.account_id)
        """,
        """
        SELECT 1 FROM observations o
        JOIN market_contexts c ON c.id = o.market_context_id
        WHERE (o.workspace_id, o.account_id) IS DISTINCT FROM
              (c.workspace_id, c.account_id)
        """,
        """
        SELECT 1 FROM observations o
        JOIN evidence_items e ON e.id = o.evidence_id
        WHERE (o.workspace_id, o.account_id) IS DISTINCT FROM
              (e.workspace_id, e.account_id)
        """,
        """
        SELECT 1 FROM order_intents i
        JOIN trades t ON t.id = i.trade_id
        WHERE (i.workspace_id, i.account_id) IS DISTINCT FROM
              (t.workspace_id, t.account_id)
        """,
        """
        SELECT 1 FROM order_intents i
        JOIN trade_plans p ON p.id = i.trade_plan_id
        WHERE (i.workspace_id, i.account_id) IS DISTINCT FROM
              (p.workspace_id, p.account_id)
        """,
        """
        SELECT 1 FROM execution_events e
        JOIN broker_connections c ON c.id = e.connection_id
        WHERE (e.workspace_id, e.account_id) IS DISTINCT FROM
              (c.workspace_id, c.account_id)
        """,
        """
        SELECT 1 FROM execution_events e
        JOIN trades t ON t.id = e.trade_id
        WHERE (e.workspace_id, e.account_id) IS DISTINCT FROM
              (t.workspace_id, t.account_id)
        """,
        """
        SELECT 1 FROM execution_events e
        JOIN order_intents i ON i.id = e.order_intent_id
        WHERE (e.workspace_id, e.account_id) IS DISTINCT FROM
              (i.workspace_id, i.account_id)
        """,
        """
        SELECT 1 FROM fills f
        JOIN execution_events e ON e.id = f.execution_event_id
        WHERE (f.workspace_id, f.account_id) IS DISTINCT FROM
              (e.workspace_id, e.account_id)
        """,
        """
        SELECT 1 FROM fills f
        JOIN trades t ON t.id = f.trade_id
        WHERE (f.workspace_id, f.account_id) IS DISTINCT FROM
              (t.workspace_id, t.account_id)
        """,
        """
        SELECT 1 FROM trade_management_events m
        JOIN order_intents i ON i.id = m.order_intent_id
        WHERE (m.workspace_id, m.account_id) IS DISTINCT FROM
              (i.workspace_id, i.account_id)
        """,
        """
        SELECT 1 FROM trade_management_events m
        JOIN execution_events e ON e.id = m.execution_event_id
        WHERE (m.workspace_id, m.account_id) IS DISTINCT FROM
              (e.workspace_id, e.account_id)
        """,
        """
        SELECT 1 FROM position_snapshots s
        JOIN trades t ON t.id = s.trade_id
        WHERE (s.workspace_id, s.account_id) IS DISTINCT FROM
              (t.workspace_id, t.account_id)
        """,
        """
        SELECT 1 FROM account_snapshots s
        JOIN execution_events e ON e.id = s.execution_event_id
        WHERE (s.workspace_id, s.account_id) IS DISTINCT FROM
              (e.workspace_id, e.account_id)
        """,
        """
        SELECT 1 FROM trade_reflections r
        JOIN trades t ON t.id = r.lifecycle_trade_id
        WHERE (r.workspace_id, r.account_id) IS DISTINCT FROM
              (t.workspace_id, t.account_id)
        """,
        """
        SELECT 1 FROM conversation_turns t
        JOIN conversation_sessions s ON s.id = t.session_id
        WHERE (t.workspace_id, t.account_id) IS DISTINCT FROM
              (s.workspace_id, s.account_id)
        """,
    )
    _assert_no_rows(
        " UNION ALL ".join(f"({query})" for query in mismatch_checks),
        "legacy relationships disagree about workspace/account ownership",
    )


def _drop_replaced_constraints() -> None:
    old_foreign_keys = {
        "account_constraint_profiles": ("account_constraint_profiles_profile_id_fkey",),
        "account_snapshots": (
            "account_snapshots_account_id_fkey",
            "account_snapshots_execution_event_id_fkey",
        ),
        "analysis_runs": (
            "analysis_runs_evidence_id_fkey",
            "analysis_runs_trade_plan_id_fkey",
        ),
        "broker_connections": ("broker_connections_account_id_fkey",),
        "connector_cursors": ("connector_cursors_connection_id_fkey",),
        "conversation_sessions": ("fk_conversation_active_playbook_version",),
        "conversation_turns": (
            "conversation_turns_session_id_fkey",
            "fk_conversation_turns_playbook_version_id_playbook_versions",
        ),
        "evidence_items": (
            "evidence_items_market_context_id_fkey",
            "evidence_items_trade_id_fkey",
            "evidence_items_trade_plan_id_fkey",
        ),
        "execution_events": (
            "execution_events_connection_id_fkey",
            "execution_events_order_intent_id_fkey",
            "execution_events_trade_id_fkey",
        ),
        "fills": (
            "fills_connection_id_fkey",
            "fills_execution_event_id_fkey",
            "fills_trade_id_fkey",
        ),
        "instrument_specifications": ("instrument_specifications_account_id_fkey",),
        "knowledge_imports": ("knowledge_imports_playbook_version_id_fkey",),
        "market_contexts": ("market_contexts_trade_plan_id_fkey",),
        "mindset_checkins": (
            "fk_mindset_checkins_playbook_version_id",
            "mindset_checkins_trade_plan_id_fkey",
        ),
        "observations": (
            "observations_evidence_id_fkey",
            "observations_market_context_id_fkey",
            "observations_trade_plan_id_fkey",
        ),
        "order_approvals": ("order_approvals_order_intent_id_fkey",),
        "order_intents": (
            "order_intents_trade_id_fkey",
            "order_intents_trade_plan_id_fkey",
        ),
        "playbook_versions": ("playbook_versions_playbook_id_fkey",),
        "position_snapshots": (
            "position_snapshots_account_id_fkey",
            "position_snapshots_trade_id_fkey",
        ),
        "pretrade_assessments": (
            "fk_pretrade_assessments_account_constraint_profile_id",
            "pretrade_assessments_mindset_checkin_id_fkey",
            "pretrade_assessments_playbook_version_id_fkey",
            "pretrade_assessments_trade_plan_id_fkey",
        ),
        "rule_evaluations": (
            "rule_evaluations_playbook_version_id_fkey",
            "rule_evaluations_reflection_id_fkey",
        ),
        "strategy_experiments": ("strategy_experiments_playbook_version_id_fkey",),
        "strategy_knowledge_items": (
            "strategy_knowledge_items_import_id_fkey",
            "strategy_knowledge_items_playbook_version_id_fkey",
        ),
        "strategy_test_samples": ("strategy_test_samples_experiment_id_fkey",),
        "trade_management_events": (
            "trade_management_events_execution_event_id_fkey",
            "trade_management_events_order_intent_id_fkey",
            "trade_management_events_trade_id_fkey",
        ),
        "trade_plans": (
            "trade_plans_account_id_fkey",
            "trade_plans_playbook_version_id_fkey",
            "trade_plans_trade_id_fkey",
        ),
        "trade_reflections": (
            "trade_reflections_lifecycle_trade_id_fkey",
            "trade_reflections_trade_id_fkey",
        ),
        "trades": ("trades_account_id_fkey",),
    }
    for table, constraints in old_foreign_keys.items():
        for constraint in constraints:
            op.drop_constraint(constraint, table, type_="foreignkey")

    old_uniques = {
        "account_constraint_profiles": ("uq_account_constraint_profile_name",),
        "broker_connections": ("uq_broker_connection_account",),
        "connector_cursors": ("uq_connector_cursor_stream",),
        "evidence_items": ("uq_evidence_content_location",),
        "execution_events": ("uq_execution_event_external",),
        "fills": ("uq_fill_external",),
        "instrument_specifications": ("uq_instrument_specification_effective",),
        "knowledge_imports": ("uq_knowledge_import_strategy_source",),
        "market_contexts": ("uq_market_context_observation",),
        "order_approvals": ("uq_order_approval_intent",),
        "order_intents": ("uq_order_intent_idempotency",),
        "trade_plans": ("uq_trade_plan_reference",),
        "trade_reflections": ("trade_reflections_trade_id_key",),
        "trader_profiles": ("uq_trader_profile_key",),
        "trades": ("uq_trade_external",),
        "trading_accounts": ("uq_trading_account_external",),
        "tradingview_alerts": (
            "uq_tradingview_alert_external_event",
            "uq_tradingview_alert_payload",
        ),
    }
    for table, constraints in old_uniques.items():
        for constraint in constraints:
            op.drop_constraint(constraint, table, type_="unique")

    op.drop_index(
        "uq_account_constraint_profile_active",
        table_name="account_constraint_profiles",
    )
    op.drop_index("uq_playbooks_name_casefold", table_name="playbooks")
    op.drop_index("ix_playbooks_name", table_name="playbooks")
    op.drop_index("ix_conversation_sessions_name", table_name="conversation_sessions")


def _enforce_columns_and_indexes() -> None:
    op.alter_column("trading_accounts", "workspace_id", nullable=False)
    op.alter_column("trading_accounts", "is_default", nullable=False)
    for table in WORKSPACE_ONLY_TABLES:
        op.alter_column(table, "workspace_id", nullable=False)
    for table, columns in ACCOUNT_SCOPE_COLUMNS.items():
        for column in columns:
            op.alter_column(table, column, nullable=False)
    op.alter_column("trade_plans", "account_id", nullable=False)

    op.create_index("ix_trading_accounts_workspace_id", "trading_accounts", ["workspace_id"])
    op.create_index("ix_trading_accounts_is_default", "trading_accounts", ["is_default"])
    op.create_index(
        "uq_trading_account_workspace_default",
        "trading_accounts",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "ix_instrument_specifications_workspace_id",
        "instrument_specifications",
        ["workspace_id"],
    )
    for table in WORKSPACE_ONLY_TABLES:
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])
    for table, columns in ACCOUNT_SCOPE_COLUMNS.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])

    op.create_index("ix_playbooks_name", "playbooks", ["name"])
    op.create_index("ix_conversation_sessions_name", "conversation_sessions", ["name"])
    op.create_index("ix_trade_reflections_trade_id", "trade_reflections", ["trade_id"])
    op.create_index(
        "ix_pretrade_scope_recall",
        "pretrade_assessments",
        ["workspace_id", "account_id", "playbook_version_id", "setup_key", "created_at"],
    )
    op.create_index(
        "ix_turn_scope_history",
        "conversation_turns",
        [
            "workspace_id",
            "account_id",
            "session_id",
            "playbook_version_id",
            "created_at",
        ],
    )
    op.create_index(
        "uq_account_constraint_profile_active",
        "account_constraint_profiles",
        ["workspace_id", "trading_account_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "uq_instrument_specification_global_effective",
        "instrument_specifications",
        ["instrument_mapping_id", "effective_from"],
        unique=True,
        postgresql_where=sa.text("account_id IS NULL"),
    )
    op.create_index(
        "uq_instrument_specification_account_effective",
        "instrument_specifications",
        ["workspace_id", "account_id", "instrument_mapping_id", "effective_from"],
        unique=True,
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )


def _create_scoped_integrity() -> None:
    unique_constraints = {
        "trading_accounts": (
            ("uq_trading_account_external", ("workspace_id", "broker", "external_account_id")),
            ("uq_trading_account_workspace_id", ("workspace_id", "id")),
        ),
        "broker_connections": (
            ("uq_broker_connection_account", ("workspace_id", "provider", "account_id")),
            ("uq_broker_connection_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "playbooks": (
            ("uq_playbook_workspace_name", ("workspace_id", "name")),
            ("uq_playbook_workspace_id", ("workspace_id", "id")),
        ),
        "playbook_versions": (
            ("uq_playbook_version_workspace_id", ("workspace_id", "id")),
        ),
        "trader_profiles": (
            ("uq_trader_profile_key", ("workspace_id", "account_id", "profile_key")),
            ("uq_trader_profile_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "account_constraint_profiles": (
            (
                "uq_account_constraint_profile_name",
                ("workspace_id", "profile_id", "trading_account_id", "name"),
            ),
            (
                "uq_account_constraint_scope_id",
                ("workspace_id", "trading_account_id", "id"),
            ),
        ),
        "knowledge_imports": (
            (
                "uq_knowledge_import_strategy_source",
                ("workspace_id", "playbook_version_id", "source_hash"),
            ),
            ("uq_knowledge_import_workspace_id", ("workspace_id", "id")),
            (
                "uq_knowledge_import_workspace_version_id",
                ("workspace_id", "id", "playbook_version_id"),
            ),
        ),
        "strategy_experiments": (
            ("uq_strategy_experiment_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "trades": (
            ("uq_trade_external", ("workspace_id", "account_id", "external_trade_id")),
            ("uq_trade_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "trade_plans": (
            ("uq_trade_plan_reference", ("workspace_id", "account_id", "reference")),
            ("uq_trade_plan_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "order_intents": (
            (
                "uq_order_intent_idempotency",
                ("workspace_id", "account_id", "idempotency_key"),
            ),
            ("uq_order_intent_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "order_approvals": (
            (
                "uq_order_approval_intent",
                ("workspace_id", "account_id", "order_intent_id"),
            ),
        ),
        "execution_events": (
            (
                "uq_execution_event_external",
                ("workspace_id", "account_id", "connection_id", "external_event_id"),
            ),
            ("uq_execution_event_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "fills": (
            (
                "uq_fill_external",
                ("workspace_id", "account_id", "connection_id", "external_fill_id"),
            ),
        ),
        "market_contexts": (
            (
                "uq_market_context_observation",
                (
                    "workspace_id",
                    "account_id",
                    "source",
                    "instrument_id",
                    "timeframe",
                    "market_time",
                ),
            ),
            ("uq_market_context_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "tradingview_alerts": (
            (
                "uq_tradingview_alert_external_event",
                ("workspace_id", "account_id", "external_event_id"),
            ),
            (
                "uq_tradingview_alert_payload",
                ("workspace_id", "account_id", "payload_sha256"),
            ),
        ),
        "evidence_items": (
            (
                "uq_evidence_content_location",
                ("workspace_id", "account_id", "sha256", "storage_uri"),
            ),
            ("uq_evidence_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "trade_reflections": (
            ("uq_trade_reflection_scope_id", ("workspace_id", "account_id", "id")),
            (
                "uq_trade_reflection_scope_trade",
                ("workspace_id", "account_id", "trade_id"),
            ),
        ),
        "mindset_checkins": (
            ("uq_mindset_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "conversation_sessions": (
            (
                "uq_conversation_workspace_account_name",
                ("workspace_id", "account_id", "name"),
            ),
            ("uq_conversation_scope_id", ("workspace_id", "account_id", "id")),
        ),
        "connector_cursors": (
            (
                "uq_connector_cursor_stream",
                ("workspace_id", "account_id", "connection_id", "stream_name"),
            ),
        ),
    }
    for table, definitions in unique_constraints.items():
        for name, columns in definitions:
            op.create_unique_constraint(name, table, list(columns))

    for table in WORKSPACE_FK_TABLES:
        op.create_foreign_key(
            f"fk_{table}_workspace",
            table,
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )

    composite_foreign_keys = (
        ("broker_connections", "fk_broker_connection_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("instrument_specifications", "fk_instrument_specification_workspace_account",
         "trading_accounts", ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("connector_cursors", "fk_connector_cursor_scope_connection", "broker_connections",
         ("workspace_id", "account_id", "connection_id"),
         ("workspace_id", "account_id", "id"), "CASCADE"),
        ("playbook_versions", "fk_playbook_version_workspace_playbook", "playbooks",
         ("workspace_id", "playbook_id"), ("workspace_id", "id"), "CASCADE"),
        ("trader_profiles", "fk_trader_profile_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("account_constraint_profiles", "fk_account_constraint_workspace_account",
         "trading_accounts", ("workspace_id", "trading_account_id"),
         ("workspace_id", "id"), "CASCADE"),
        ("account_constraint_profiles", "fk_account_constraint_scope_profile",
         "trader_profiles", ("workspace_id", "trading_account_id", "profile_id"),
         ("workspace_id", "account_id", "id"), "CASCADE"),
        ("knowledge_imports", "fk_knowledge_import_workspace_version", "playbook_versions",
         ("workspace_id", "playbook_version_id"), ("workspace_id", "id"), "CASCADE"),
        ("strategy_knowledge_items", "fk_strategy_knowledge_workspace_import_version",
         "knowledge_imports", ("workspace_id", "import_id", "playbook_version_id"),
         ("workspace_id", "id", "playbook_version_id"), "CASCADE"),
        ("strategy_experiments", "fk_strategy_experiment_workspace_account",
         "trading_accounts", ("workspace_id", "account_id"), ("workspace_id", "id"), "CASCADE"),
        ("strategy_experiments", "fk_strategy_experiment_workspace_version",
         "playbook_versions", ("workspace_id", "playbook_version_id"),
         ("workspace_id", "id"), "CASCADE"),
        ("strategy_test_samples", "fk_strategy_test_sample_scope_experiment",
         "strategy_experiments", ("workspace_id", "account_id", "experiment_id"),
         ("workspace_id", "account_id", "id"), "CASCADE"),
        ("trades", "fk_trade_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("trade_plans", "fk_trade_plan_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("trade_plans", "fk_trade_plan_workspace_version", "playbook_versions",
         ("workspace_id", "playbook_version_id"), ("workspace_id", "id"), None),
        ("trade_plans", "fk_trade_plan_scope_trade", "trades",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("order_intents", "fk_order_intent_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("order_intents", "fk_order_intent_scope_trade", "trades",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("order_intents", "fk_order_intent_scope_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_plan_id"),
         ("workspace_id", "account_id", "id"), None),
        ("order_approvals", "fk_order_approval_scope_intent", "order_intents",
         ("workspace_id", "account_id", "order_intent_id"),
         ("workspace_id", "account_id", "id"), "CASCADE"),
        ("execution_events", "fk_execution_event_scope_connection", "broker_connections",
         ("workspace_id", "account_id", "connection_id"),
         ("workspace_id", "account_id", "id"), None),
        ("execution_events", "fk_execution_event_scope_trade", "trades",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("execution_events", "fk_execution_event_scope_intent", "order_intents",
         ("workspace_id", "account_id", "order_intent_id"),
         ("workspace_id", "account_id", "id"), None),
        ("fills", "fk_fill_scope_connection", "broker_connections",
         ("workspace_id", "account_id", "connection_id"),
         ("workspace_id", "account_id", "id"), None),
        ("fills", "fk_fill_scope_trade", "trades",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("fills", "fk_fill_scope_execution_event", "execution_events",
         ("workspace_id", "account_id", "execution_event_id"),
         ("workspace_id", "account_id", "id"), None),
        ("trade_management_events", "fk_management_scope_trade", "trades",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("trade_management_events", "fk_management_scope_intent", "order_intents",
         ("workspace_id", "account_id", "order_intent_id"),
         ("workspace_id", "account_id", "id"), None),
        ("trade_management_events", "fk_management_scope_execution_event", "execution_events",
         ("workspace_id", "account_id", "execution_event_id"),
         ("workspace_id", "account_id", "id"), None),
        ("position_snapshots", "fk_position_snapshot_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("position_snapshots", "fk_position_snapshot_scope_trade", "trades",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("account_snapshots", "fk_account_snapshot_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("account_snapshots", "fk_account_snapshot_scope_execution_event", "execution_events",
         ("workspace_id", "account_id", "execution_event_id"),
         ("workspace_id", "account_id", "id"), None),
        ("market_contexts", "fk_market_context_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("market_contexts", "fk_market_context_scope_trade_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_plan_id"),
         ("workspace_id", "account_id", "id"), None),
        ("tradingview_alerts", "fk_tradingview_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("evidence_items", "fk_evidence_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("evidence_items", "fk_evidence_scope_trade", "trades",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("evidence_items", "fk_evidence_scope_trade_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_plan_id"),
         ("workspace_id", "account_id", "id"), None),
        ("evidence_items", "fk_evidence_scope_market_context", "market_contexts",
         ("workspace_id", "account_id", "market_context_id"),
         ("workspace_id", "account_id", "id"), None),
        ("analysis_runs", "fk_analysis_run_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("analysis_runs", "fk_analysis_run_scope_evidence", "evidence_items",
         ("workspace_id", "account_id", "evidence_id"),
         ("workspace_id", "account_id", "id"), None),
        ("analysis_runs", "fk_analysis_run_scope_trade_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_plan_id"),
         ("workspace_id", "account_id", "id"), None),
        ("observations", "fk_observation_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("observations", "fk_observation_scope_trade_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_plan_id"),
         ("workspace_id", "account_id", "id"), None),
        ("observations", "fk_observation_scope_market_context", "market_contexts",
         ("workspace_id", "account_id", "market_context_id"),
         ("workspace_id", "account_id", "id"), None),
        ("observations", "fk_observation_scope_evidence", "evidence_items",
         ("workspace_id", "account_id", "evidence_id"),
         ("workspace_id", "account_id", "id"), None),
        ("trade_reflections", "fk_trade_reflection_scope_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_id"),
         ("workspace_id", "account_id", "id"), "CASCADE"),
        ("trade_reflections", "fk_trade_reflection_scope_trade", "trades",
         ("workspace_id", "account_id", "lifecycle_trade_id"),
         ("workspace_id", "account_id", "id"), None),
        ("rule_evaluations", "fk_rule_evaluation_scope_reflection", "trade_reflections",
         ("workspace_id", "account_id", "reflection_id"),
         ("workspace_id", "account_id", "id"), "CASCADE"),
        ("rule_evaluations", "fk_rule_evaluation_workspace_version", "playbook_versions",
         ("workspace_id", "playbook_version_id"), ("workspace_id", "id"), None),
        ("mindset_checkins", "fk_mindset_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("mindset_checkins", "fk_mindset_workspace_version", "playbook_versions",
         ("workspace_id", "playbook_version_id"), ("workspace_id", "id"), None),
        ("mindset_checkins", "fk_mindset_scope_trade_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_plan_id"),
         ("workspace_id", "account_id", "id"), None),
        ("pretrade_assessments", "fk_pretrade_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("pretrade_assessments", "fk_pretrade_workspace_version", "playbook_versions",
         ("workspace_id", "playbook_version_id"), ("workspace_id", "id"), None),
        ("pretrade_assessments", "fk_pretrade_scope_mindset", "mindset_checkins",
         ("workspace_id", "account_id", "mindset_checkin_id"),
         ("workspace_id", "account_id", "id"), None),
        ("pretrade_assessments", "fk_pretrade_scope_constraint",
         "account_constraint_profiles",
         ("workspace_id", "account_id", "account_constraint_profile_id"),
         ("workspace_id", "trading_account_id", "id"), None),
        ("pretrade_assessments", "fk_pretrade_scope_trade_plan", "trade_plans",
         ("workspace_id", "account_id", "trade_plan_id"),
         ("workspace_id", "account_id", "id"), None),
        ("conversation_sessions", "fk_conversation_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("conversation_sessions", "fk_conversation_workspace_version", "playbook_versions",
         ("workspace_id", "active_playbook_version_id"), ("workspace_id", "id"), None),
        ("conversation_turns", "fk_turn_workspace_account", "trading_accounts",
         ("workspace_id", "account_id"), ("workspace_id", "id"), None),
        ("conversation_turns", "fk_turn_scope_session", "conversation_sessions",
         ("workspace_id", "account_id", "session_id"),
         ("workspace_id", "account_id", "id"), "CASCADE"),
        ("conversation_turns", "fk_turn_workspace_version", "playbook_versions",
         ("workspace_id", "playbook_version_id"), ("workspace_id", "id"), None),
    )
    for (
        source_table,
        name,
        target_table,
        source_columns,
        target_columns,
        ondelete,
    ) in composite_foreign_keys:
        op.create_foreign_key(
            name,
            source_table,
            target_table,
            list(source_columns),
            list(target_columns),
            ondelete=ondelete,
        )

    op.create_check_constraint(
        "ck_instrument_specification_scope_pair",
        "instrument_specifications",
        "(workspace_id IS NULL) = (account_id IS NULL)",
    )


def upgrade() -> None:
    _add_scope_columns()
    _backfill_legacy_scope()
    _validate_backfill()
    _drop_replaced_constraints()
    _enforce_columns_and_indexes()
    _create_scoped_integrity()


def downgrade() -> None:
    raise RuntimeError(
        "a73f1c9d4e20 is an account-isolation boundary and cannot be downgraded "
        "automatically without collapsing account-specific profiles and evidence"
    )
