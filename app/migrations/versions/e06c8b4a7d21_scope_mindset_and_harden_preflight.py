"""scope mindset records and harden preflight decisions

Revision ID: e06c8b4a7d21
Revises: d95b7a2e4f10
Create Date: 2026-07-25 00:00:01.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e06c8b4a7d21"
down_revision: str | Sequence[str] | None = "d95b7a2e4f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mindset_checkins",
        sa.Column("playbook_version_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_mindset_checkins_playbook_version_id",
        "mindset_checkins",
        "playbook_versions",
        ["playbook_version_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_mindset_checkins_playbook_version_id"),
        "mindset_checkins",
        ["playbook_version_id"],
        unique=False,
    )
    op.execute(
        """
        UPDATE mindset_checkins AS mindset
        SET playbook_version_id = trade.playbook_version_id
        FROM trade_plans AS trade
        WHERE mindset.trade_plan_id = trade.id
          AND trade.playbook_version_id IS NOT NULL
        """
    )
    op.create_check_constraint(
        "ck_pretrade_assessment_proceed_eligible",
        "pretrade_assessments",
        "human_decision != 'proceed' "
        "OR (rating IN ('eligible', 'conditional') AND trade_plan_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_pretrade_assessment_trade_decision",
        "pretrade_assessments",
        "human_decision = 'proceed' OR trade_plan_id IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pretrade_assessment_trade_decision",
        "pretrade_assessments",
        type_="check",
    )
    op.drop_constraint(
        "ck_pretrade_assessment_proceed_eligible",
        "pretrade_assessments",
        type_="check",
    )
    op.drop_index(
        op.f("ix_mindset_checkins_playbook_version_id"),
        table_name="mindset_checkins",
    )
    op.drop_constraint(
        "fk_mindset_checkins_playbook_version_id",
        "mindset_checkins",
        type_="foreignkey",
    )
    op.drop_column("mindset_checkins", "playbook_version_id")
