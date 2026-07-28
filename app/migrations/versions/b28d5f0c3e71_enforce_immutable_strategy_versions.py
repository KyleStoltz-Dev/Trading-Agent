"""enforce immutable strategy versions

Revision ID: b28d5f0c3e71
Revises: a17c4e9b2d60
Create Date: 2026-07-26 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b28d5f0c3e71"
down_revision: str | Sequence[str] | None = "a17c4e9b2d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_playbooks_name_casefold "
        "ON playbooks (lower(name))"
    )
    op.execute(
        """
        CREATE FUNCTION reject_playbook_version_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'playbook versions are immutable; create a new version instead';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_playbook_versions_immutable
        BEFORE UPDATE OR DELETE ON playbook_versions
        FOR EACH ROW
        EXECUTE FUNCTION reject_playbook_version_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_playbook_versions_immutable "
        "ON playbook_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_playbook_version_mutation()")
    op.execute("DROP INDEX IF EXISTS uq_playbooks_name_casefold")
