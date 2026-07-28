"""add trader learning curricula

Revision ID: f21a7c9e4d10
Revises: e06c8b4a7d21
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f21a7c9e4d10"
down_revision: str | Sequence[str] | None = "e06c8b4a7d21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learning_curricula",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("experience_level", sa.String(length=40), nullable=False),
        sa.Column("teaching_mode", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "selected_topics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "source_tier_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "teaching_mode IN ('guided', 'flexible', 'on_demand')",
            name="ck_learning_curriculum_teaching_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'completed')",
            name="ck_learning_curriculum_status",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["trader_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", name="uq_learning_curriculum_profile"),
    )
    for column in ("experience_level", "profile_id", "status", "teaching_mode"):
        op.create_index(
            op.f(f"ix_learning_curricula_{column}"),
            "learning_curricula",
            [column],
            unique=False,
        )

    op.create_table(
        "learning_modules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("curriculum_id", sa.UUID(), nullable=False),
        sa.Column("module_key", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("framework", sa.String(length=80), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "objectives",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "source_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_references",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("learner_notes", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('available', 'in_progress', 'completed', 'skipped')",
            name="ck_learning_module_status",
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_learning_module_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["curriculum_id"],
            ["learning_curricula.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "curriculum_id",
            "module_key",
            name="uq_learning_module_curriculum_key",
        ),
    )
    for column in ("category", "curriculum_id", "framework", "module_key", "status"):
        op.create_index(
            op.f(f"ix_learning_modules_{column}"),
            "learning_modules",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("learning_modules")
    op.drop_table("learning_curricula")
