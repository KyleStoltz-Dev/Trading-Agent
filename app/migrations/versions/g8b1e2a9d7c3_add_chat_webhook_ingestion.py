"""add chat webhook ingestion tables

Revision ID: g8b1e2a9d7c3
Revises: f33a7c1e6d42
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g8b1e2a9d7c3"
down_revision: str | Sequence[str] | None = "f33a7c1e6d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trading_accounts",
        sa.Column(
            "telegram_webhook_secret_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "trading_accounts",
        sa.Column(
            "discord_webhook_secret_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )

    op.create_table(
        "chat_webhook_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("external_message_id", sa.String(length=160), nullable=False),
        sa.Column("sender_id", sa.String(length=120), nullable=False),
        sa.Column("sender_name", sa.String(length=160), nullable=True),
        sa.Column("channel_id", sa.String(length=120), nullable=True),
        sa.Column("channel_name", sa.String(length=200), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("verified_source", sa.String(length=120), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_accounts.workspace_id", "trading_accounts.id"],
            name="fk_chat_webhook_workspace_account",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "platform",
            "external_message_id",
            name="uq_chat_webhook_message_external",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "payload_sha256",
            name="uq_chat_webhook_message_payload",
        ),
    )
    op.create_index(
        "ix_chat_webhook_platform_time",
        "chat_webhook_messages",
        ["workspace_id", "account_id", "platform", "sent_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_webhook_platform_time", table_name="chat_webhook_messages")
    op.drop_table("chat_webhook_messages")
    op.drop_column("trading_accounts", "discord_webhook_secret_sha256")
    op.drop_column("trading_accounts", "telegram_webhook_secret_sha256")
