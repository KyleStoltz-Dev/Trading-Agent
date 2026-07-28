"""capture broker fill costs

Revision ID: a17c4e9b2d60
Revises: f32b8d0a5e11
Create Date: 2026-07-26 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a17c4e9b2d60"
down_revision: str | Sequence[str] | None = "f32b8d0a5e11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fills",
        sa.Column(
            "guaranteed_execution_fee",
            sa.Numeric(precision=24, scale=4),
            nullable=True,
        ),
    )
    op.add_column(
        "fills",
        sa.Column(
            "half_spread_cost",
            sa.Numeric(precision=24, scale=4),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("fills", "half_spread_cost")
    op.drop_column("fills", "guaranteed_execution_fee")
