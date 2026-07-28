"""audit mutating tool executions and conversation outcomes

Revision ID: a84d7e2c1f90
Revises: a73f1c9d4e20
Create Date: 2026-07-27 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a84d7e2c1f90"
down_revision: str | Sequence[str] | None = "a73f1c9d4e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_turns",
        sa.Column("request_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "conversation_turns",
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="complete",
            nullable=False,
        ),
    )
    op.add_column(
        "conversation_turns",
        sa.Column("error_type", sa.String(length=120), nullable=True),
    )
    op.create_check_constraint(
        "ck_turn_status",
        "conversation_turns",
        "status IN ('pending', 'complete', 'partial', 'failed')",
    )
    op.create_unique_constraint(
        "uq_conversation_turn_scope_id",
        "conversation_turns",
        ["workspace_id", "account_id", "id"],
    )
    op.create_unique_constraint(
        "uq_conversation_turn_request_role",
        "conversation_turns",
        ["workspace_id", "account_id", "session_id", "request_id", "role"],
    )
    op.create_index(
        "ix_conversation_turns_request_id",
        "conversation_turns",
        ["request_id"],
    )
    op.create_index(
        "ix_conversation_turns_status",
        "conversation_turns",
        ["status"],
    )
    op.alter_column("conversation_turns", "status", server_default=None)

    op.create_table(
        "tool_execution_audits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("conversation_session_id", sa.UUID(), nullable=True),
        sa.Column("user_turn_id", sa.UUID(), nullable=True),
        sa.Column("playbook_version_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_type", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'declined', 'succeeded', 'failed')",
            name="ck_tool_execution_status",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_tool_execution_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_accounts.workspace_id", "trading_accounts.id"],
            name="fk_tool_execution_workspace_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "conversation_session_id"],
            [
                "conversation_sessions.workspace_id",
                "conversation_sessions.account_id",
                "conversation_sessions.id",
            ],
            name="fk_tool_execution_scope_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "user_turn_id"],
            [
                "conversation_turns.workspace_id",
                "conversation_turns.account_id",
                "conversation_turns.id",
            ],
            name="fk_tool_execution_scope_turn",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "playbook_version_id"],
            ["playbook_versions.workspace_id", "playbook_versions.id"],
            name="fk_tool_execution_workspace_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "request_id",
            "tool_name",
            "arguments_hash",
            name="uq_tool_execution_request_arguments",
        ),
    )
    op.create_index(
        "ix_tool_execution_scope_request",
        "tool_execution_audits",
        ["workspace_id", "account_id", "request_id", "created_at"],
    )
    for column in (
        "conversation_session_id",
        "user_turn_id",
        "playbook_version_id",
    ):
        op.create_index(
            f"ix_tool_execution_audits_{column}",
            "tool_execution_audits",
            [column],
        )


def downgrade() -> None:
    op.drop_table("tool_execution_audits")
    op.drop_index(
        "ix_conversation_turns_status",
        table_name="conversation_turns",
    )
    op.drop_index(
        "ix_conversation_turns_request_id",
        table_name="conversation_turns",
    )
    op.drop_constraint(
        "uq_conversation_turn_request_role",
        "conversation_turns",
        type_="unique",
    )
    op.drop_constraint(
        "uq_conversation_turn_scope_id",
        "conversation_turns",
        type_="unique",
    )
    op.drop_constraint("ck_turn_status", "conversation_turns", type_="check")
    op.drop_column("conversation_turns", "error_type")
    op.drop_column("conversation_turns", "status")
    op.drop_column("conversation_turns", "request_id")
