"""add per-account TradingView webhook secret

Revision ID: b95e3a7d2f01
Revises: a84d7e2c1f90
Create Date: 2026-07-27 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b95e3a7d2f01"
down_revision: str | Sequence[str] | None = "a84d7e2c1f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trading_accounts",
        sa.Column(
            "tradingview_webhook_secret_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("trading_accounts", "tradingview_webhook_secret_sha256")
