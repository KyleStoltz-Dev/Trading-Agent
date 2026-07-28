"""drop redundant tool audit indexes

Revision ID: c06d8f1a4b27
Revises: b95e3a7d2f01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c06d8f1a4b27"
down_revision: str | None = "b95e3a7d2f01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REDUNDANT_INDEXES = (
    "ix_tool_execution_audits_account_id",
    "ix_tool_execution_audits_created_at",
    "ix_tool_execution_audits_request_id",
    "ix_tool_execution_audits_status",
    "ix_tool_execution_audits_tool_name",
    "ix_tool_execution_audits_workspace_id",
)


def upgrade() -> None:
    # Early local builds of the unmerged audit migration created these indexes.
    # Fresh installs do not, so the cleanup must be safe in both states.
    for name in REDUNDANT_INDEXES:
        op.drop_index(name, table_name="tool_execution_audits", if_exists=True)


def downgrade() -> None:
    for name in REDUNDANT_INDEXES:
        column = name.removeprefix("ix_tool_execution_audits_")
        op.create_index(
            name,
            "tool_execution_audits",
            [column],
            if_not_exists=True,
        )
