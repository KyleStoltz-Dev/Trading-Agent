import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

PRICE = Numeric(24, 10)
QUANTITY = Numeric(24, 10)
MONEY = Numeric(24, 4)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("canonical_symbol", "asset_class", name="uq_instrument_identity"),
        CheckConstraint("price_precision >= 0", name="ck_instrument_price_precision"),
        CheckConstraint("quantity_precision >= 0", name="ck_instrument_quantity_precision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_symbol: Mapped[str] = mapped_column(String(40), index=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    asset_class: Mapped[str] = mapped_column(String(32), index=True)
    base_currency: Mapped[str | None] = mapped_column(String(12))
    quote_currency: Mapped[str | None] = mapped_column(String(12))
    price_precision: Mapped[int] = mapped_column(Integer, default=5)
    quantity_precision: Mapped[int] = mapped_column(Integer, default=2)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradingAccount(Base):
    __tablename__ = "trading_accounts"
    __table_args__ = (
        UniqueConstraint("broker", "external_account_id", name="uq_trading_account_external"),
        CheckConstraint("mode IN ('practice', 'live', 'backtest')", name="ck_account_mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker: Mapped[str] = mapped_column(String(40), index=True)
    external_account_id: Mapped[str] = mapped_column(String(160))
    label: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(12))
    mode: Mapped[str] = mapped_column(String(16), default="practice")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrokerConnection(Base):
    __tablename__ = "broker_connections"
    __table_args__ = (
        UniqueConstraint("provider", "account_id", name="uq_broker_connection_account"),
        CheckConstraint(
            "status IN ('configured', 'healthy', 'degraded', 'disabled')",
            name="ck_broker_connection_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trading_accounts.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), index=True)
    environment: Mapped[str] = mapped_column(String(32), default="practice")
    status: Mapped[str] = mapped_column(String(16), default="configured")
    config_reference: Mapped[str | None] = mapped_column(
        String(255), comment="Secret-store reference only; never the credential itself."
    )
    last_healthy_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped[TradingAccount] = relationship()


class InstrumentMapping(Base):
    __tablename__ = "instrument_mappings"
    __table_args__ = (
        UniqueConstraint("provider", "external_symbol", name="uq_instrument_mapping_external"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), index=True)
    external_symbol: Mapped[str] = mapped_column(String(80), index=True)
    venue: Mapped[str | None] = mapped_column(String(80))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    instrument: Mapped[Instrument] = relationship()


class InstrumentSpecification(Base):
    __tablename__ = "instrument_specifications"
    __table_args__ = (
        UniqueConstraint(
            "instrument_mapping_id",
            "effective_from",
            name="uq_instrument_specification_effective",
        ),
        CheckConstraint("tick_size > 0", name="ck_instrument_spec_tick_size"),
        CheckConstraint(
            "tick_value_per_quantity_unit > 0",
            name="ck_instrument_spec_tick_value",
        ),
        CheckConstraint("contract_size > 0", name="ck_instrument_spec_contract_size"),
        CheckConstraint("minimum_quantity > 0", name="ck_instrument_spec_minimum"),
        CheckConstraint(
            "maximum_quantity >= minimum_quantity",
            name="ck_instrument_spec_maximum",
        ),
        CheckConstraint("quantity_step > 0", name="ck_instrument_spec_step"),
        CheckConstraint(
            "margin_rate IS NULL OR (margin_rate > 0 AND margin_rate <= 1)",
            name="ck_instrument_spec_margin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instrument_mapping_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instrument_mappings.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trading_accounts.id"), index=True
    )
    contract_size: Mapped[Decimal] = mapped_column(QUANTITY)
    tick_size: Mapped[Decimal] = mapped_column(PRICE)
    tick_value_per_quantity_unit: Mapped[Decimal] = mapped_column(MONEY)
    minimum_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    maximum_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    quantity_step: Mapped[Decimal] = mapped_column(QUANTITY)
    margin_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    estimated_spread: Mapped[Decimal | None] = mapped_column(PRICE)
    commission_per_quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    financing_per_quantity_day: Mapped[Decimal | None] = mapped_column(MONEY)
    pnl_currency: Mapped[str] = mapped_column(String(12))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(80))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectorCursor(Base):
    __tablename__ = "connector_cursors"
    __table_args__ = (
        UniqueConstraint("connection_id", "stream_name", name="uq_connector_cursor_stream"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )
    stream_name: Mapped[str] = mapped_column(String(80))
    cursor_value: Mapped[str] = mapped_column(String(255))
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Playbook(Base):
    __tablename__ = "playbooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlaybookVersion(Base):
    __tablename__ = "playbook_versions"
    __table_args__ = (
        UniqueConstraint("playbook_id", "version", name="uq_playbook_version"),
        CheckConstraint("version > 0", name="ck_playbook_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("playbooks.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSONB)
    change_hypothesis: Mapped[str | None] = mapped_column(Text)
    sample_requirement: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(80), default="human")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("account_id", "external_trade_id", name="uq_trade_external"),
        CheckConstraint("direction IN ('long', 'short')", name="ck_trade_direction"),
        CheckConstraint(
            "status IN ('planned', 'open', 'partially_closed', 'closed', 'cancelled')",
            name="ck_trade_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trading_accounts.id"), index=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    external_trade_id: Mapped[str | None] = mapped_column(String(160))
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(24), default="planned", index=True)
    origin: Mapped[str] = mapped_column(String(24), default="manual")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TradePlan(Base):
    __tablename__ = "trade_plans"
    __table_args__ = (
        CheckConstraint("direction IN ('long', 'short')", name="ck_trade_plan_direction"),
        CheckConstraint(
            "status IN ('draft', 'planned', 'invalidated', 'executed', 'reviewed')",
            name="ck_trade_plan_status",
        ),
        CheckConstraint("risk_percent > 0", name="ck_trade_plan_risk_percent"),
        CheckConstraint("risk_amount > 0", name="ck_trade_plan_risk_amount"),
        CheckConstraint("quantity > 0", name="ck_trade_plan_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trading_accounts.id"), index=True
    )
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("playbook_versions.id"), index=True
    )
    instrument_specification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instrument_specifications.id"), index=True
    )
    instrument: Mapped[str] = mapped_column(String(32), index=True)
    venue: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    setup_name: Mapped[str] = mapped_column(String(120), index=True)
    regime: Mapped[str | None] = mapped_column(String(64), index=True)
    session_name: Mapped[str | None] = mapped_column(String(40), index=True)
    context_timeframe: Mapped[str] = mapped_column(String(16))
    trigger_timeframe: Mapped[str] = mapped_column(String(16))
    entry: Mapped[Decimal] = mapped_column(PRICE)
    stop: Mapped[Decimal] = mapped_column(PRICE)
    target: Mapped[Decimal] = mapped_column(PRICE)
    account_equity: Mapped[Decimal] = mapped_column(MONEY)
    risk_percent: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    value_per_price_unit: Mapped[Decimal] = mapped_column(PRICE)
    risk_amount: Mapped[Decimal] = mapped_column(MONEY)
    estimated_costs: Mapped[Decimal | None] = mapped_column(MONEY)
    estimated_margin: Mapped[Decimal | None] = mapped_column(MONEY)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    planned_r: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    thesis: Mapped[str] = mapped_column(Text)
    invalidation: Mapped[str] = mapped_column(Text)
    observations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    interpretations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    minutes_to_high_impact_event: Mapped[int | None] = mapped_column(Integer)
    policy_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="planned", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    reflection: Mapped["TradeReflection | None"] = relationship(
        back_populates="trade_plan", cascade="all, delete-orphan", uselist=False
    )


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_order_intent_idempotency"),
        CheckConstraint(
            "action IN ('open', 'reduce', 'close', 'modify_stop', 'modify_target', 'hedge')",
            name="ck_order_intent_action",
        ),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_order_intent_side"),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'expired', 'submitted', 'failed')",
            name="ck_order_intent_status",
        ),
        CheckConstraint("quantity > 0", name="ck_order_intent_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), index=True
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id"), index=True
    )
    action: Mapped[str] = mapped_column(String(24))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(24))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    target_price: Mapped[Decimal | None] = mapped_column(PRICE)
    time_in_force: Mapped[str | None] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    policy_hash: Mapped[str] = mapped_column(String(64))
    proposed_by: Mapped[str] = mapped_column(String(24))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class OrderApproval(Base):
    __tablename__ = "order_approvals"
    __table_args__ = (
        UniqueConstraint("order_intent_id", name="uq_order_approval_intent"),
        CheckConstraint("decision IN ('approved', 'rejected')", name="ck_order_approval_decision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_intents.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[str] = mapped_column(String(120))
    channel: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    intent_hash: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_event_id", name="uq_execution_event_external"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_connections.id"), index=True
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), index=True
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_intents.id"), index=True
    )
    external_event_id: Mapped[str] = mapped_column(String(160))
    external_order_id: Mapped[str | None] = mapped_column(String(160), index=True)
    external_trade_id: Mapped[str | None] = mapped_column(String(160), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    source_payload_hash: Mapped[str | None] = mapped_column(String(64))
    provider_metadata: Mapped[dict] = mapped_column(
        JSONB, default=dict, comment="Sanitized metadata only; no credentials or full payload."
    )


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_fill_id", name="uq_fill_external"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_fill_side"),
        CheckConstraint("quantity > 0", name="ck_fill_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_connections.id"), index=True
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), index=True
    )
    execution_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("execution_events.id"), index=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    external_fill_id: Mapped[str] = mapped_column(String(160))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    commission: Mapped[Decimal | None] = mapped_column(MONEY)
    financing: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TradeManagementEvent(Base):
    __tablename__ = "trade_management_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'partial_taken', 'stop_moved', 'target_moved', 'breakeven_set', "
            "'runner_left', 'hedge_considered', 'hedge_taken', 'manual_close', 'note'"
            ")",
            name="ck_trade_management_event_type",
        ),
        CheckConstraint(
            "actor_type IN ('human', 'agent', 'import', 'system')",
            name="ck_trade_management_actor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), index=True
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_intents.id"), index=True
    )
    execution_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("execution_events.id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    quantity_delta: Mapped[Decimal | None] = mapped_column(QUANTITY)
    position_quantity_after: Mapped[Decimal | None] = mapped_column(QUANTITY)
    realized_r_at_event: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    reason: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('fill', 'management', 'review', 'manual', 'reconciliation')",
            name="ck_position_snapshot_trigger",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trading_accounts.id"), index=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(24))
    net_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    average_price: Mapped[Decimal | None] = mapped_column(PRICE)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40))


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('fill', 'management', 'review', 'manual', 'reconciliation')",
            name="ck_account_snapshot_trigger",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trading_accounts.id"), index=True
    )
    execution_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("execution_events.id"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(24))
    currency: Mapped[str] = mapped_column(String(12))
    balance: Mapped[Decimal] = mapped_column(MONEY)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    margin_used: Mapped[Decimal | None] = mapped_column(MONEY)
    margin_available: Mapped[Decimal | None] = mapped_column(MONEY)
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40))


class MarketContext(Base):
    __tablename__ = "market_contexts"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "instrument_id",
            "timeframe",
            "market_time",
            name="uq_market_context_observation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(40), index=True)
    venue: Mapped[str] = mapped_column(String(80))
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    facts: Mapped[list] = mapped_column(JSONB, default=list)
    hypotheses: Mapped[list] = mapped_column(JSONB, default=list)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)


class EconomicEvent(Base):
    __tablename__ = "economic_events"
    __table_args__ = (
        UniqueConstraint("source", "source_event_id", name="uq_economic_event_source"),
        CheckConstraint("importance BETWEEN 0 AND 3", name="ck_economic_event_importance"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String(80), index=True)
    source_event_id: Mapped[str] = mapped_column(String(160))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    country: Mapped[str] = mapped_column(String(120), index=True)
    currency: Mapped[str | None] = mapped_column(String(12), index=True)
    category: Mapped[str | None] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(Integer, default=0, index=True)
    actual: Mapped[str | None] = mapped_column(String(120))
    forecast: Mapped[str | None] = mapped_column(String(120))
    previous: Mapped[str | None] = mapped_column(String(120))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("source", "source_item_id", name="uq_news_item_source"),
        CheckConstraint("importance BETWEEN 0 AND 3", name="ck_news_item_importance"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String(80), index=True)
    source_item_id: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(120), index=True)
    category: Mapped[str | None] = mapped_column(String(160), index=True)
    symbol: Mapped[str | None] = mapped_column(String(120), index=True)
    importance: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        UniqueConstraint("sha256", "storage_uri", name="uq_evidence_content_location"),
        CheckConstraint(
            "evidence_type IN ('chart', 'news', 'calendar', 'note', 'broker_record')",
            name="ck_evidence_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), index=True
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id"), index=True
    )
    market_context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("market_contexts.id"), index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(24))
    storage_uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(80))
    source_reference: Mapped[str | None] = mapped_column(Text)
    market_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        CheckConstraint(
            "analysis_type IN ('chart', 'news', 'market', 'review')",
            name="ck_analysis_run_type",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed', 'corrected')",
            name="ck_analysis_run_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_items.id"), index=True
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id"), index=True
    )
    analysis_type: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    policy_hash: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    output_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_type: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('fact', 'hypothesis', 'question', 'confirmation')",
            name="ck_observation_kind",
        ),
        CheckConstraint(
            "actor_type IN ('human', 'agent', 'import', 'system')",
            name="ck_observation_actor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id"), index=True
    )
    market_context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("market_contexts.id"), index=True
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_items.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), index=True)
    text: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradeReflection(Base):
    __tablename__ = "trade_reflections"
    __table_args__ = (
        CheckConstraint("execution_grade IN ('A', 'B', 'C', 'D', 'F')", name="ck_review_grade"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id", ondelete="CASCADE"), unique=True
    )
    lifecycle_trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), index=True
    )
    exit_average: Mapped[Decimal] = mapped_column(PRICE)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    realized_r: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    execution_grade: Mapped[str] = mapped_column(String(8))
    outcome_grade: Mapped[str | None] = mapped_column(String(16))
    process_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    outcome_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    maximum_favorable_excursion_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    maximum_adverse_excursion_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    total_fees: Mapped[Decimal | None] = mapped_column(MONEY)
    slippage_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    rule_adherence: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    emotion_before: Mapped[str | None] = mapped_column(Text)
    emotion_during: Mapped[str | None] = mapped_column(Text)
    emotion_after: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trade_plan: Mapped[TradePlan] = relationship(back_populates="reflection")


class RuleEvaluation(Base):
    __tablename__ = "rule_evaluations"
    __table_args__ = (
        CheckConstraint(
            "result IN ('met', 'not_met', 'unclear', 'not_applicable')",
            name="ck_rule_evaluation_result",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reflection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_reflections.id", ondelete="CASCADE"), index=True
    )
    playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("playbook_versions.id"), index=True
    )
    rule_key: Mapped[str] = mapped_column(String(120), index=True)
    result: Mapped[str] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)


class MindsetCheckIn(Base):
    __tablename__ = "mindset_checkins"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('pre_session', 'pre_trade', 'during_trade', 'post_trade')",
            name="ck_mindset_phase",
        ),
        CheckConstraint("readiness BETWEEN 1 AND 5", name="ck_mindset_readiness"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_plans.id"), index=True
    )
    phase: Mapped[str] = mapped_column(String(24))
    readiness: Mapped[int] = mapped_column(Integer)
    accepted_risk: Mapped[bool] = mapped_column(Boolean)
    emotion_tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160), default="Trading Agent session")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )

    turns: Mapped[list["ConversationTurn"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ConversationTurn.created_at",
    )


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_turn_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    session: Mapped[ConversationSession] = relationship(back_populates="turns")
