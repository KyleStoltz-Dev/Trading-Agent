"""audit secret cleanup failures

Revision ID: e22f6b0d5c31
Revises: d17e5a9c4b20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e22f6b0d5c31"
down_revision: str | Sequence[str] | None = "d17e5a9c4b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_security_audit_action",
        "security_audit_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_audit_action",
        "security_audit_events",
        "action IN ('credential_created', 'credential_rotated', "
        "'credential_removed', 'credential_cleanup_failed', "
        "'principal_granted', 'principal_revoked')",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM security_audit_events "
        "WHERE action = 'credential_cleanup_failed'"
    )
    op.drop_constraint(
        "ck_security_audit_action",
        "security_audit_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_audit_action",
        "security_audit_events",
        "action IN ('credential_created', 'credential_rotated', "
        "'credential_removed', 'principal_granted', 'principal_revoked')",
    )
