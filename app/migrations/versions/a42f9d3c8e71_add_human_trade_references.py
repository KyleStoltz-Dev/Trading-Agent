"""add human-readable trade-plan references

Revision ID: a42f9d3c8e71
Revises: 7b1e2c9d4a6f
Create Date: 2026-07-25 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a42f9d3c8e71"
down_revision: str | Sequence[str] | None = "7b1e2c9d4a6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_plans",
        sa.Column("reference", sa.String(length=120), nullable=True),
    )
    op.execute(
        """
        UPDATE trade_plans
        SET reference =
            lower(regexp_replace(instrument, '[^a-zA-Z0-9]+', '', 'g'))
            || '-' || to_char(coalesce(source_time, created_at), 'YYYYMMDD')
            || '-' || direction
            || '-' || substring(id::text, 1, 8)
        """
    )
    op.alter_column("trade_plans", "reference", nullable=False)
    op.create_unique_constraint(
        "uq_trade_plan_reference",
        "trade_plans",
        ["reference"],
    )
    op.create_index(
        op.f("ix_trade_plans_reference"),
        "trade_plans",
        ["reference"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_trade_plans_reference"), table_name="trade_plans")
    op.drop_constraint("uq_trade_plan_reference", "trade_plans", type_="unique")
    op.drop_column("trade_plans", "reference")
