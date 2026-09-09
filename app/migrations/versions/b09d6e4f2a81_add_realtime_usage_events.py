"""add realtime usage events

Revision ID: b09d6e4f2a81
Revises: a91d4e6f2b73
"""

import sqlalchemy as sa
from alembic import op

revision: str = "b09d6e4f2a81"
down_revision: str | None = "a91d4e6f2b73"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "realtime_usage_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("response_id", sa.String(length=200), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("input_text_tokens", sa.Integer(), nullable=False),
        sa.Column("input_audio_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_text_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_audio_tokens", sa.Integer(), nullable=False),
        sa.Column("output_text_tokens", sa.Integer(), nullable=False),
        sa.Column("output_audio_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "input_text_tokens >= 0 AND input_audio_tokens >= 0 "
            "AND cached_text_tokens >= 0 AND cached_audio_tokens >= 0 "
            "AND output_text_tokens >= 0 AND output_audio_tokens >= 0 "
            "AND estimated_cost_usd >= 0",
            name="ck_realtime_usage_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_accounts.workspace_id", "trading_accounts.id"],
            name="fk_realtime_usage_workspace_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id", "session_id"],
            [
                "conversation_sessions.workspace_id",
                "conversation_sessions.account_id",
                "conversation_sessions.id",
            ],
            name="fk_realtime_usage_scope_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "session_id",
            "response_id",
            name="uq_realtime_usage_response",
        ),
    )
    op.create_index(
        "ix_realtime_usage_scope_time",
        "realtime_usage_events",
        ["workspace_id", "account_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_realtime_usage_events_workspace_id"),
        "realtime_usage_events",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_realtime_usage_events_account_id"),
        "realtime_usage_events",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_realtime_usage_events_session_id"),
        "realtime_usage_events",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_realtime_usage_events_created_at"),
        "realtime_usage_events",
        ["created_at"],
        unique=False,
    )
    op.execute("ALTER TABLE public.realtime_usage_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_scope ON public.realtime_usage_events
          USING (
            workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
            AND account_id = NULLIF(current_setting('app.account_id', true), '')::uuid
          )
          WITH CHECK (
            workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
            AND account_id = NULLIF(current_setting('app.account_id', true), '')::uuid
          )
        """
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_realtime_usage_events_created_at"),
        table_name="realtime_usage_events",
    )
    op.drop_index(
        op.f("ix_realtime_usage_events_session_id"),
        table_name="realtime_usage_events",
    )
    op.drop_index(
        op.f("ix_realtime_usage_events_account_id"),
        table_name="realtime_usage_events",
    )
    op.drop_index(
        op.f("ix_realtime_usage_events_workspace_id"),
        table_name="realtime_usage_events",
    )
    op.drop_index("ix_realtime_usage_scope_time", table_name="realtime_usage_events")
    op.drop_table("realtime_usage_events")
