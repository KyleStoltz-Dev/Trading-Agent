"""add auditable pretrade assessments

Revision ID: d95b7a2e4f10
Revises: c84a6f1d9e32
Create Date: 2026-07-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d95b7a2e4f10"
down_revision: str | Sequence[str] | None = "c84a6f1d9e32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pretrade_assessments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("playbook_version_id", sa.UUID(), nullable=False),
        sa.Column("mindset_checkin_id", sa.UUID(), nullable=True),
        sa.Column("trade_plan_id", sa.UUID(), nullable=True),
        sa.Column("setup_key", sa.String(length=120), nullable=True),
        sa.Column("rating", sa.String(length=24), nullable=False),
        sa.Column(
            "component_scores",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "hard_blockers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "stand_aside_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "missing_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "rule_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("news_status", sa.String(length=24), nullable=False),
        sa.Column(
            "market_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("human_decision", sa.String(length=24), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "rating IN ('eligible', 'conditional', 'stand_aside', 'blocked')",
            name="ck_pretrade_assessment_rating",
        ),
        sa.CheckConstraint(
            "human_decision IN ('pending', 'proceed', 'stand_aside', 'cancelled')",
            name="ck_pretrade_assessment_decision",
        ),
        sa.CheckConstraint(
            "news_status IN ('fresh', 'stale', 'not_configured', 'unavailable')",
            name="ck_pretrade_assessment_news_status",
        ),
        sa.ForeignKeyConstraint(
            ["mindset_checkin_id"],
            ["mindset_checkins.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["playbook_version_id"],
            ["playbook_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["trade_plan_id"],
            ["trade_plans.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pretrade_assessments_created_at"),
        "pretrade_assessments",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pretrade_assessments_human_decision"),
        "pretrade_assessments",
        ["human_decision"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pretrade_assessments_mindset_checkin_id"),
        "pretrade_assessments",
        ["mindset_checkin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pretrade_assessments_playbook_version_id"),
        "pretrade_assessments",
        ["playbook_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pretrade_assessments_rating"),
        "pretrade_assessments",
        ["rating"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pretrade_assessments_trade_plan_id"),
        "pretrade_assessments",
        ["trade_plan_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_pretrade_assessments_trade_plan_id"),
        table_name="pretrade_assessments",
    )
    op.drop_index(
        op.f("ix_pretrade_assessments_rating"),
        table_name="pretrade_assessments",
    )
    op.drop_index(
        op.f("ix_pretrade_assessments_playbook_version_id"),
        table_name="pretrade_assessments",
    )
    op.drop_index(
        op.f("ix_pretrade_assessments_mindset_checkin_id"),
        table_name="pretrade_assessments",
    )
    op.drop_index(
        op.f("ix_pretrade_assessments_human_decision"),
        table_name="pretrade_assessments",
    )
    op.drop_index(
        op.f("ix_pretrade_assessments_created_at"),
        table_name="pretrade_assessments",
    )
    op.drop_table("pretrade_assessments")
