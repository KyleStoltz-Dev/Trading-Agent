"""add mindset emotional state

Revision ID: c39e6a1f4b82
Revises: b28d5f0c3e71
Create Date: 2026-07-26 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c39e6a1f4b82"
down_revision: str | Sequence[str] | None = "b28d5f0c3e71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mindset_checkins",
        sa.Column("emotional_state", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mindset_checkins", "emotional_state")
