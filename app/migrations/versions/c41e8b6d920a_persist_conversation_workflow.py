"""Persist account/strategy-scoped workflow checkpoints on conversation turns.

Revision ID: c41e8b6d920a
Revises: b09d6e4f2a81
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c41e8b6d920a"
down_revision = "b09d6e4f2a81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing turn scope, foreign keys and tenant RLS also protect this column.
    # An open terminal transaction must not make startup wait indefinitely.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("conversation_turns", sa.Column(
        "workflow_checkpoint", postgresql.JSONB(none_as_null=True), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("conversation_turns", "workflow_checkpoint")
