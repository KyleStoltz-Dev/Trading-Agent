"""track selected learning modules

Revision ID: f32b8d0a5e11
Revises: f21a7c9e4d10
Create Date: 2026-07-26 00:00:01.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f32b8d0a5e11"
down_revision: str | Sequence[str] | None = "f21a7c9e4d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "learning_modules",
        sa.Column(
            "included",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_learning_modules_included"),
        "learning_modules",
        ["included"],
        unique=False,
    )
    op.alter_column("learning_modules", "included", server_default=None)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_learning_modules_included"),
        table_name="learning_modules",
    )
    op.drop_column("learning_modules", "included")
