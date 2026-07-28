"""add trader account constraint profiles

Revision ID: d40f7b2a6c91
Revises: c39e6a1f4b82
Create Date: 2026-07-26 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d40f7b2a6c91"
down_revision: str | Sequence[str] | None = "c39e6a1f4b82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_constraint_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("account_type", sa.String(length=16), nullable=False),
        sa.Column("account_size", sa.Numeric(precision=24, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=12), nullable=False),
        sa.Column("firm_name", sa.String(length=120), nullable=True),
        sa.Column("program_name", sa.String(length=120), nullable=True),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column(
            "rule_limits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
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
            "account_type IN ('personal', 'prop')",
            name="ck_account_constraint_type",
        ),
        sa.CheckConstraint(
            "phase IN ('personal', 'evaluation', 'verification', 'funded')",
            name="ck_account_constraint_phase",
        ),
        sa.CheckConstraint(
            "account_size > 0",
            name="ck_account_constraint_size",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["trader_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "name",
            name="uq_account_constraint_profile_name",
        ),
    )
    for column in ("account_type", "active", "phase", "profile_id"):
        op.create_index(
            op.f(f"ix_account_constraint_profiles_{column}"),
            "account_constraint_profiles",
            [column],
            unique=False,
        )
    op.add_column(
        "pretrade_assessments",
        sa.Column("account_constraint_profile_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pretrade_assessments_account_constraint_profile_id",
        "pretrade_assessments",
        "account_constraint_profiles",
        ["account_constraint_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_pretrade_assessments_account_constraint_profile_id"),
        "pretrade_assessments",
        ["account_constraint_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_pretrade_assessments_account_constraint_profile_id"),
        table_name="pretrade_assessments",
    )
    op.drop_constraint(
        "fk_pretrade_assessments_account_constraint_profile_id",
        "pretrade_assessments",
        type_="foreignkey",
    )
    op.drop_column(
        "pretrade_assessments",
        "account_constraint_profile_id",
    )
    op.drop_table("account_constraint_profiles")
