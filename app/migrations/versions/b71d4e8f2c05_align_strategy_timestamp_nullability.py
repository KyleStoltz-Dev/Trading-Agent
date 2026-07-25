"""align strategy timestamp nullability

Revision ID: b71d4e8f2c05
Revises: a42f9d3c8e71
Create Date: 2026-07-25 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b71d4e8f2c05"
down_revision: str | Sequence[str] | None = "a42f9d3c8e71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMP_COLUMNS = (
    ("knowledge_imports", "imported_at"),
    ("strategy_experiments", "created_at"),
    ("strategy_knowledge_items", "created_at"),
    ("strategy_test_samples", "created_at"),
    ("trader_profiles", "created_at"),
    ("trader_profiles", "updated_at"),
)


def upgrade() -> None:
    for table, column in TIMESTAMP_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    for table, column in reversed(TIMESTAMP_COLUMNS):
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )
