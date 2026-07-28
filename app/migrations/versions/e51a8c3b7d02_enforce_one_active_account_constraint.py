"""enforce one active account constraint per trader

Revision ID: e51a8c3b7d02
Revises: d40f7b2a6c91
Create Date: 2026-07-26 23:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e51a8c3b7d02"
down_revision: str | Sequence[str] | None = "d40f7b2a6c91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_account_constraint_profile_active",
        "account_constraint_profiles",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_account_constraint_profile_active",
        table_name="account_constraint_profiles",
    )
