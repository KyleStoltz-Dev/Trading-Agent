"""sync chat webhook metadata and indexes

Revision ID: ff1b7e2c4d55
Revises: g8b1e2a9d7c3
"""

from alembic import op

revision: str = "ff1b7e2c4d55"
down_revision: str | None = "g8b1e2a9d7c3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_chat_webhook_messages_workspace_id",
        "chat_webhook_messages",
        ["workspace_id"],
    )
    op.create_index(
        "ix_chat_webhook_messages_account_id",
        "chat_webhook_messages",
        ["account_id"],
    )
    op.create_index(
        "ix_chat_webhook_messages_external_message_id",
        "chat_webhook_messages",
        ["external_message_id"],
    )
    op.create_index(
        "ix_chat_webhook_messages_platform",
        "chat_webhook_messages",
        ["platform"],
    )
    op.create_index(
        "ix_chat_webhook_messages_sent_at",
        "chat_webhook_messages",
        ["sent_at"],
    )
    op.create_index(
        "ix_chat_webhook_messages_received_at",
        "chat_webhook_messages",
        ["received_at"],
    )

    op.create_foreign_key(
        "chat_webhook_messages_workspace_id_fkey",
        "chat_webhook_messages",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute(
        """
        ALTER TABLE public.chat_webhook_messages ENABLE ROW LEVEL SECURITY
        """
    )
    op.execute(
        """
        DROP POLICY IF EXISTS tenant_scope ON public.chat_webhook_messages
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_scope ON public.chat_webhook_messages
          USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
          WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
        """
    )

    op.alter_column(
        "trading_accounts",
        "telegram_webhook_secret_sha256",
        comment="SHA-256 digest of the account-specific Telegram webhook secret.",
    )
    op.alter_column(
        "trading_accounts",
        "discord_webhook_secret_sha256",
        comment="SHA-256 digest of the account-specific Discord webhook secret.",
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS tenant_scope ON public.chat_webhook_messages
        """
    )
    op.execute(
        """
        ALTER TABLE public.chat_webhook_messages DISABLE ROW LEVEL SECURITY
        """
    )
    op.alter_column("trading_accounts", "discord_webhook_secret_sha256", comment=None)
    op.alter_column("trading_accounts", "telegram_webhook_secret_sha256", comment=None)
    op.drop_constraint(
        "chat_webhook_messages_workspace_id_fkey",
        "chat_webhook_messages",
        type_="foreignkey",
    )
    op.drop_index("ix_chat_webhook_messages_received_at", table_name="chat_webhook_messages")
    op.drop_index("ix_chat_webhook_messages_sent_at", table_name="chat_webhook_messages")
    op.drop_index("ix_chat_webhook_messages_platform", table_name="chat_webhook_messages")
    op.drop_index(
        "ix_chat_webhook_messages_external_message_id",
        table_name="chat_webhook_messages",
    )
    op.drop_index("ix_chat_webhook_messages_account_id", table_name="chat_webhook_messages")
    op.drop_index("ix_chat_webhook_messages_workspace_id", table_name="chat_webhook_messages")
