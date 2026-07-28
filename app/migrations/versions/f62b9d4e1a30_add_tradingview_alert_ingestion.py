"""add verified TradingView alert ingestion

Revision ID: f62b9d4e1a30
Revises: e51a8c3b7d02
Create Date: 2026-07-27 00:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f62b9d4e1a30"
down_revision: str | Sequence[str] | None = "e51a8c3b7d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tradingview_alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("external_event_id", sa.String(length=160), nullable=False),
        sa.Column("alert_name", sa.String(length=160), nullable=False),
        sa.Column("symbol", sa.String(length=80), nullable=False),
        sa.Column("exchange", sa.String(length=80), nullable=True),
        sa.Column("timeframe", sa.String(length=24), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("condition", sa.String(length=300), nullable=True),
        sa.Column("market_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("open_price", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("high_price", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("low_price", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("close_price", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("volume", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("verified_source_ip", sa.String(length=45), nullable=False),
        sa.Column("verification_method", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "external_event_id",
            name="uq_tradingview_alert_external_event",
        ),
        sa.UniqueConstraint(
            "payload_sha256",
            name="uq_tradingview_alert_payload",
        ),
    )
    for column in (
        "event_type",
        "exchange",
        "market_time",
        "received_at",
        "symbol",
        "timeframe",
    ):
        op.create_index(
            op.f(f"ix_tradingview_alerts_{column}"),
            "tradingview_alerts",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("tradingview_alerts")
