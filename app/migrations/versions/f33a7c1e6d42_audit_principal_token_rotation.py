"""audit principal token rotation

Revision ID: f33a7c1e6d42
Revises: e22f6b0d5c31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f33a7c1e6d42"
down_revision: str | Sequence[str] | None = "e22f6b0d5c31"
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
        "'principal_granted', 'principal_token_rotated', 'principal_revoked')",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM security_audit_events "
        "WHERE action = 'principal_token_rotated'"
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
        "'credential_removed', 'credential_cleanup_failed', "
        "'principal_granted', 'principal_revoked')",
    )
