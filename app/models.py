import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TradePlan(Base):
    __tablename__ = "trade_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instrument: Mapped[str] = mapped_column(String(32), index=True)
    venue: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    setup_name: Mapped[str] = mapped_column(String(120), index=True)
    regime: Mapped[str | None] = mapped_column(String(64), index=True)
    context_timeframe: Mapped[str] = mapped_column(String(16))
    trigger_timeframe: Mapped[str] = mapped_column(String(16))
    entry: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    stop: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    target: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    account_equity: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    risk_percent: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    value_per_price_unit: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    planned_r: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    thesis: Mapped[str] = mapped_column(Text)
    invalidation: Mapped[str] = mapped_column(Text)
    observations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    interpretations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(24), default="planned", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    reflection: Mapped["TradeReflection | None"] = relationship(
        back_populates="trade", cascade="all, delete-orphan", uselist=False
    )


class TradeReflection(Base):
    __tablename__ = "trade_reflections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id", ondelete="CASCADE"), unique=True
    )
    exit_average: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    realized_r: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    execution_grade: Mapped[str] = mapped_column(String(8))
    rule_adherence: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    emotion_before: Mapped[str | None] = mapped_column(Text)
    emotion_during: Mapped[str | None] = mapped_column(Text)
    emotion_after: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    trade: Mapped[TradePlan] = relationship(back_populates="reflection")

