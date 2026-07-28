"""hosted identity, secret audit, and tenant RLS

Revision ID: d17e5a9c4b20
Revises: c06d8f1a4b27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d17e5a9c4b20"
down_revision: str | Sequence[str] | None = "c06d8f1a4b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_principals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.String(length=160), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject", name="uq_api_principal_subject"),
        sa.UniqueConstraint("token_sha256", name="uq_api_principal_token_sha256"),
    )
    op.create_index("ix_api_principals_active", "api_principals", ["active"])
    op.create_table(
        "api_principal_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="reader"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('reader', 'trader', 'admin')", name="ck_api_grant_role"
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["api_principals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_accounts.workspace_id", "trading_accounts.id"],
            name="fk_api_principal_grant_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_id",
            "workspace_id",
            "account_id",
            name="uq_api_principal_account_grant",
        ),
    )
    op.create_index(
        "ix_api_principal_grants_principal_id",
        "api_principal_grants",
        ["principal_id"],
    )
    op.create_index(
        "ix_api_principal_grants_workspace_id",
        "api_principal_grants",
        ["workspace_id"],
    )
    op.create_index(
        "ix_api_principal_grants_account_id",
        "api_principal_grants",
        ["account_id"],
    )
    op.create_table(
        "security_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("secret_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('credential_created', 'credential_rotated', "
            "'credential_removed', 'principal_granted', 'principal_revoked')",
            name="ck_security_audit_action",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_accounts.workspace_id", "trading_accounts.id"],
            name="fk_security_audit_event_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_security_audit_scope_created",
        "security_audit_events",
        ["workspace_id", "account_id", "created_at"],
    )

    # Policies use transaction-local settings supplied by app.db.get_db. Principal
    # bootstrap tables are intentionally excluded so authentication can happen first.
    op.execute(
        """
        DO $rls$
        DECLARE item record;
        DECLARE predicate text;
        BEGIN
          FOR item IN
            SELECT c.table_name,
                   bool_or(c.column_name = 'workspace_id') AS has_workspace,
                   bool_or(c.column_name = 'account_id') AS has_account
              FROM information_schema.columns c
             WHERE c.table_schema = 'public'
               AND c.table_name NOT IN
                   ('alembic_version', 'api_principals', 'api_principal_grants')
             GROUP BY c.table_name
          LOOP
            IF item.table_name = 'workspaces' THEN
              predicate := 'id = NULLIF(current_setting('
                || '''app.workspace_id'', true), '''')::uuid';
            ELSIF item.table_name = 'trading_accounts' THEN
              predicate := 'workspace_id = NULLIF(current_setting('
                || '''app.workspace_id'', true), '''')::uuid'
                || ' AND id = NULLIF(current_setting('
                || '''app.account_id'', true), '''')::uuid';
            ELSIF item.has_workspace AND item.has_account THEN
              predicate := 'workspace_id = NULLIF(current_setting('
                || '''app.workspace_id'', true), '''')::uuid'
                || ' AND account_id = NULLIF(current_setting('
                || '''app.account_id'', true), '''')::uuid';
            ELSIF item.has_workspace THEN
              predicate := 'workspace_id = NULLIF(current_setting('
                || '''app.workspace_id'', true), '''')::uuid';
            ELSE
              CONTINUE;
            END IF;
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', item.table_name);
            EXECUTE format(
              'CREATE POLICY tenant_scope ON public.%I USING (%s) WITH CHECK (%s)',
              item.table_name, predicate, predicate
            );
          END LOOP;
        END
        $rls$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $rls$
        DECLARE item record;
        BEGIN
          FOR item IN
            SELECT schemaname, tablename
              FROM pg_policies
             WHERE schemaname = 'public' AND policyname = 'tenant_scope'
          LOOP
            EXECUTE format(
              'DROP POLICY tenant_scope ON public.%I', item.tablename
            );
            EXECUTE format(
              'ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', item.tablename
            );
          END LOOP;
        END
        $rls$;
        """
    )
    op.drop_table("security_audit_events")
    op.drop_table("api_principal_grants")
    op.drop_table("api_principals")
