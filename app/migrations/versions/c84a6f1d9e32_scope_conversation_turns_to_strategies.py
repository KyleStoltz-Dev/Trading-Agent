"""scope conversation turns to immutable strategy versions

Revision ID: c84a6f1d9e32
Revises: b71d4e8f2c05
Create Date: 2026-07-25 21:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c84a6f1d9e32"
down_revision: str | Sequence[str] | None = "b71d4e8f2c05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_turns",
        sa.Column("playbook_version_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversation_turns_playbook_version_id_playbook_versions",
        "conversation_turns",
        "playbook_versions",
        ["playbook_version_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_conversation_turns_playbook_version_id"),
        "conversation_turns",
        ["playbook_version_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_conversation_turns_playbook_version_id"),
        table_name="conversation_turns",
    )
    op.drop_constraint(
        "fk_conversation_turns_playbook_version_id_playbook_versions",
        "conversation_turns",
        type_="foreignkey",
    )
    op.drop_column("conversation_turns", "playbook_version_id")
