"""repair chat webhook tenant policy for previously upgraded databases

Revision ID: a91d4e6f2b73
Revises: ff1b7e2c4d55
"""

from alembic import op

revision: str = "a91d4e6f2b73"
down_revision: str | None = "ff1b7e2c4d55"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    # The preceding revision declares the same policy. Keep its contract intact.
    pass
