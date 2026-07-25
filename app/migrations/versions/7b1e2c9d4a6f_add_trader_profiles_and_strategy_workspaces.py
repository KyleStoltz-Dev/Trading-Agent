"""add trader profiles and isolated strategy workspaces

Revision ID: 7b1e2c9d4a6f
Revises: 1385d072a844
Create Date: 2026-07-25 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7b1e2c9d4a6f"
down_revision: str | Sequence[str] | None = "1385d072a844"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trader_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("profile_key", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("timezone", sa.String(length=80), nullable=False),
        sa.Column("experience_level", sa.String(length=40), nullable=True),
        sa.Column("trading_style", sa.Text(), nullable=False),
        sa.Column("markets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sessions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("goals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_preferences", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("onboarding_complete", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_key", name="uq_trader_profile_key"),
    )
    op.create_index(
        op.f("ix_trader_profiles_profile_key"),
        "trader_profiles",
        ["profile_key"],
        unique=False,
    )
    op.create_table(
        "knowledge_imports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("playbook_version_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "source_type IN "
            "('discord', 'telegram', 'x', 'generic', 'file', 'directory', 'paste')",
            name="ck_knowledge_import_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'partial', 'failed')",
            name="ck_knowledge_import_status",
        ),
        sa.ForeignKeyConstraint(
            ["playbook_version_id"],
            ["playbook_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "playbook_version_id",
            "source_hash",
            name="uq_knowledge_import_strategy_source",
        ),
    )
    op.create_index(
        op.f("ix_knowledge_imports_imported_at"),
        "knowledge_imports",
        ["imported_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_imports_playbook_version_id"),
        "knowledge_imports",
        ["playbook_version_id"],
        unique=False,
    )
    op.create_table(
        "strategy_experiments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("playbook_version_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("mode", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("instrument", sa.String(length=40), nullable=True),
        sa.Column("timeframe", sa.String(length=16), nullable=True),
        sa.Column("data_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rules_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('backtest', 'forward_test')",
            name="ck_strategy_experiment_mode",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'running', 'completed', 'cancelled')",
            name="ck_strategy_experiment_status",
        ),
        sa.ForeignKeyConstraint(["playbook_version_id"], ["playbook_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("created_at", "instrument", "mode", "playbook_version_id", "status"):
        op.create_index(
            op.f(f"ix_strategy_experiments_{column}"),
            "strategy_experiments",
            [column],
            unique=False,
        )
    op.create_table(
        "strategy_knowledge_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("import_id", sa.UUID(), nullable=False),
        sa.Column("playbook_version_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=160), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "kind IN ('message', 'note', 'document', 'rule', 'example')",
            name="ck_strategy_knowledge_kind",
        ),
        sa.ForeignKeyConstraint(["import_id"], ["knowledge_imports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["playbook_version_id"],
            ["playbook_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "content_hash",
        "created_at",
        "excluded",
        "import_id",
        "kind",
        "occurred_at",
        "playbook_version_id",
    ):
        op.create_index(
            op.f(f"ix_strategy_knowledge_items_{column}"),
            "strategy_knowledge_items",
            [column],
            unique=False,
        )
    op.create_table(
        "strategy_test_samples",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instrument", sa.String(length=40), nullable=False),
        sa.Column("setup_key", sa.String(length=120), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("exclusion_reason", sa.Text(), nullable=True),
        sa.Column("outcome_r", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("process_score", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("feature_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "classification IN ('eligible', 'excluded', 'unclear')",
            name="ck_strategy_test_sample_classification",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["strategy_experiments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("created_at", "experiment_id", "instrument", "occurred_at", "setup_key"):
        op.create_index(
            op.f(f"ix_strategy_test_samples_{column}"),
            "strategy_test_samples",
            [column],
            unique=False,
        )
    op.add_column(
        "conversation_sessions",
        sa.Column("active_playbook_version_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        op.f("ix_conversation_sessions_active_playbook_version_id"),
        "conversation_sessions",
        ["active_playbook_version_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_conversation_active_playbook_version",
        "conversation_sessions",
        "playbook_versions",
        ["active_playbook_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_conversation_active_playbook_version",
        "conversation_sessions",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_conversation_sessions_active_playbook_version_id"),
        table_name="conversation_sessions",
    )
    op.drop_column("conversation_sessions", "active_playbook_version_id")
    op.drop_table("strategy_test_samples")
    op.drop_table("strategy_knowledge_items")
    op.drop_table("strategy_experiments")
    op.drop_table("knowledge_imports")
    op.drop_index(op.f("ix_trader_profiles_profile_key"), table_name="trader_profiles")
    op.drop_table("trader_profiles")
