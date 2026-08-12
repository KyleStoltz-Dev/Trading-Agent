import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

PRICE = Numeric(24, 10)
QUANTITY = Numeric(24, 10)
MONEY = Numeric(24, 4)
class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_workspace_slug"),
        CheckConstraint("slug <> ''", name="ck_workspace_slug_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    slug: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ApiPrincipal(Base):
    """Authenticated API identity; only a high-entropy bearer-token digest is stored."""

    __tablename__ = "api_principals"
    __table_args__ = (
        UniqueConstraint("subject", name="uq_api_principal_subject"),
        UniqueConstraint("token_sha256", name="uq_api_principal_token_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject: Mapped[str] = mapped_column(String(160))
    token_sha256: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApiPrincipalGrant(Base):
    """Exact account grant for one authenticated principal."""

    __tablename__ = "api_principal_grants"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "workspace_id",
            "account_id",
            name="uq_api_principal_account_grant",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_api_principal_grant_account",
            ondelete="CASCADE",
        ),
        CheckConstraint("role IN ('reader', 'trader', 'admin')", name="ck_api_grant_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_principals.id", ondelete="CASCADE"),
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    role: Mapped[str] = mapped_column(String(16), default="reader")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
        UniqueConstraint(
            "workspace_id",
            "broker",
            "external_account_id",
            name="uq_trading_account_external",
        ),
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_trading_account_workspace_id",
        ),
        Index(
            "uq_trading_account_workspace_default",
            "workspace_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        CheckConstraint("mode IN ('practice', 'live', 'backtest')", name="ck_account_mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    broker: Mapped[str] = mapped_column(String(40), index=True)
    external_account_id: Mapped[str] = mapped_column(String(160))
    label: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(12))
    mode: Mapped[str] = mapped_column(String(16), default="practice")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tradingview_webhook_secret_sha256: Mapped[str | None] = mapped_column(String(64))
    telegram_webhook_secret_sha256: Mapped[str | None] = mapped_column(
        String(64),
        comment="SHA-256 digest of the account-specific Telegram webhook secret.",
    )
    discord_webhook_secret_sha256: Mapped[str | None] = mapped_column(
        String(64),
        comment="SHA-256 digest of the account-specific Discord webhook secret.",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrokerConnection(Base):
    __tablename__ = "broker_connections"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider",
            "account_id",
            name="uq_broker_connection_account",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_broker_connection_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_broker_connection_workspace_account",
        ),
        CheckConstraint(
            "status IN ('configured', 'healthy', 'degraded', 'disabled')",
            name="ck_broker_connection_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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


class SecurityAuditEvent(Base):
    """Append-only security event containing identifiers, never secret material."""

    __tablename__ = "security_audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_security_audit_event_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "action IN ('credential_created', 'credential_rotated', "
            "'credential_removed', 'credential_cleanup_failed', "
            "'principal_granted', 'principal_token_rotated', 'principal_revoked')",
            name="ck_security_audit_action",
        ),
        Index(
            "ix_security_audit_scope_created",
            "workspace_id",
            "account_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(160))
    secret_reference: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
        Index(
            "uq_instrument_specification_global_effective",
            "instrument_mapping_id",
            "effective_from",
            unique=True,
            postgresql_where=text("account_id IS NULL"),
        ),
        Index(
            "uq_instrument_specification_account_effective",
            "workspace_id",
            "account_id",
            "instrument_mapping_id",
            "effective_from",
            unique=True,
            postgresql_where=text("account_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_instrument_specification_workspace_account",
        ),
        CheckConstraint(
            "(workspace_id IS NULL) = (account_id IS NULL)",
            name="ck_instrument_specification_scope_pair",
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    instrument_mapping_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instrument_mappings.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "connection_id",
            "stream_name",
            name="uq_connector_cursor_stream",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "connection_id"),
            (
                "broker_connections.workspace_id",
                "broker_connections.account_id",
                "broker_connections.id",
            ),
            name="fk_connector_cursor_scope_connection",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    stream_name: Mapped[str] = mapped_column(String(80))
    cursor_value: Mapped[str] = mapped_column(String(255))
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Playbook(Base):
    __tablename__ = "playbooks"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "name",
            name="uq_playbook_workspace_name",
        ),
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_playbook_workspace_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlaybookVersion(Base):
    __tablename__ = "playbook_versions"
    __table_args__ = (
        UniqueConstraint("playbook_id", "version", name="uq_playbook_version"),
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_playbook_version_workspace_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_id"),
            ("playbooks.workspace_id", "playbooks.id"),
            name="fk_playbook_version_workspace_playbook",
            ondelete="CASCADE",
        ),
        CheckConstraint("version > 0", name="ck_playbook_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSONB)
    change_hypothesis: Mapped[str | None] = mapped_column(Text)
    sample_requirement: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(80), default="human")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TraderProfile(Base):
    __tablename__ = "trader_profiles"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "profile_key",
            name="uq_trader_profile_key",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_trader_profile_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_trader_profile_workspace_account",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    profile_key: Mapped[str] = mapped_column(String(80), default="local", index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(80))
    experience_level: Mapped[str | None] = mapped_column(String(40))
    trading_style: Mapped[str] = mapped_column(Text, default="")
    markets: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sessions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    goals: Mapped[list[str]] = mapped_column(JSONB, default=list)
    risk_preferences: Mapped[dict] = mapped_column(JSONB, default=dict)
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AccountConstraintProfile(Base):
    """Trader-entered account and prop-program limits, separate from broker credentials."""

    __tablename__ = "account_constraint_profiles"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "profile_id",
            "trading_account_id",
            "name",
            name="uq_account_constraint_profile_name",
        ),
        UniqueConstraint(
            "workspace_id",
            "trading_account_id",
            "id",
            name="uq_account_constraint_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "trading_account_id", "profile_id"),
            (
                "trader_profiles.workspace_id",
                "trader_profiles.account_id",
                "trader_profiles.id",
            ),
            name="fk_account_constraint_scope_profile",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "trading_account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_account_constraint_workspace_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "account_type IN ('personal', 'prop')",
            name="ck_account_constraint_type",
        ),
        CheckConstraint(
            "phase IN ('personal', 'evaluation', 'verification', 'funded')",
            name="ck_account_constraint_phase",
        ),
        CheckConstraint("account_size > 0", name="ck_account_constraint_size"),
        Index(
            "uq_account_constraint_profile_active",
            "workspace_id",
            "trading_account_id",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    trading_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120))
    account_type: Mapped[str] = mapped_column(String(16), index=True)
    account_size: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(12))
    firm_name: Mapped[str | None] = mapped_column(String(120))
    program_name: Mapped[str | None] = mapped_column(String(120))
    phase: Mapped[str] = mapped_column(String(16), index=True)
    rule_limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class LearningCurriculum(Base):
    __tablename__ = "learning_curricula"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_learning_curriculum_profile"),
        CheckConstraint(
            "teaching_mode IN ('guided', 'flexible', 'on_demand')",
            name="ck_learning_curriculum_teaching_mode",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed')",
            name="ck_learning_curriculum_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trader_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    experience_level: Mapped[str] = mapped_column(String(40), index=True)
    teaching_mode: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    selected_topics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source_tier_policy: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LearningModule(Base):
    __tablename__ = "learning_modules"
    __table_args__ = (
        UniqueConstraint(
            "curriculum_id",
            "module_key",
            name="uq_learning_module_curriculum_key",
        ),
        CheckConstraint(
            "status IN ('available', 'in_progress', 'completed', 'skipped')",
            name="ck_learning_module_status",
        ),
        CheckConstraint("sequence > 0", name="ck_learning_module_sequence_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    curriculum_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("learning_curricula.id", ondelete="CASCADE"),
        index=True,
    )
    module_key: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40), index=True)
    framework: Mapped[str | None] = mapped_column(String(80), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    included: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="available", index=True)
    objectives: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source_plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence_references: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    learner_notes: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeImport(Base):
    __tablename__ = "knowledge_imports"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "playbook_version_id",
            "source_hash",
            name="uq_knowledge_import_strategy_source",
        ),
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_knowledge_import_workspace_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "id",
            "playbook_version_id",
            name="uq_knowledge_import_workspace_version_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_knowledge_import_workspace_version",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "source_type IN ('discord', 'telegram', 'x', 'generic', 'file', 'directory', 'paste')",
            name="ck_knowledge_import_source_type",
        ),
        CheckConstraint(
            "status IN ('completed', 'partial', 'failed')",
            name="ck_knowledge_import_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(24))
    source_name: Mapped[str] = mapped_column(String(255))
    source_locator: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="completed")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class StrategyKnowledgeItem(Base):
    __tablename__ = "strategy_knowledge_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('message', 'note', 'document', 'rule', 'example')",
            name="ck_strategy_knowledge_kind",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "import_id", "playbook_version_id"),
            (
                "knowledge_imports.workspace_id",
                "knowledge_imports.id",
                "knowledge_imports.playbook_version_id",
            ),
            name="fk_strategy_knowledge_workspace_import_version",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    import_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(24), default="document", index=True)
    source_reference: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(160))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_strategy_experiment_scope_id",
        ),
        CheckConstraint(
            "mode IN ('backtest', 'forward_test')",
            name="ck_strategy_experiment_mode",
        ),
        CheckConstraint(
            "status IN ('draft', 'running', 'completed', 'cancelled')",
            name="ck_strategy_experiment_status",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_strategy_experiment_workspace_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_strategy_experiment_workspace_version",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(160))
    mode: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    instrument: Mapped[str | None] = mapped_column(String(40), index=True)
    timeframe: Mapped[str | None] = mapped_column(String(16))
    data_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rules_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyTestSample(Base):
    __tablename__ = "strategy_test_samples"
    __table_args__ = (
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "experiment_id"),
            (
                "strategy_experiments.workspace_id",
                "strategy_experiments.account_id",
                "strategy_experiments.id",
            ),
            name="fk_strategy_test_sample_scope_experiment",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "classification IN ('eligible', 'excluded', 'unclear')",
            name="ck_strategy_test_sample_classification",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    instrument: Mapped[str] = mapped_column(String(40), index=True)
    setup_key: Mapped[str] = mapped_column(String(120), index=True)
    classification: Mapped[str] = mapped_column(String(16))
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    outcome_r: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    process_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    feature_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    source_reference: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "external_trade_id",
            name="uq_trade_external",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_trade_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_trade_workspace_account",
        ),
        CheckConstraint("direction IN ('long', 'short')", name="ck_trade_direction"),
        CheckConstraint(
            "status IN ('planned', 'open', 'partially_closed', 'closed', 'cancelled')",
            name="ck_trade_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "reference",
            name="uq_trade_plan_reference",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_trade_plan_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_trade_plan_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_trade_plan_workspace_version",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_trade_plan_scope_trade",
        ),
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
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    reference: Mapped[str] = mapped_column(String(120), index=True)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "idempotency_key",
            name="uq_order_intent_idempotency",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_order_intent_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_order_intent_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_order_intent_scope_trade",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_plan_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_order_intent_scope_plan",
        ),
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
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "order_intent_id",
            name="uq_order_approval_intent",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "order_intent_id"),
            (
                "order_intents.workspace_id",
                "order_intents.account_id",
                "order_intents.id",
            ),
            name="fk_order_approval_scope_intent",
            ondelete="CASCADE",
        ),
        CheckConstraint("decision IN ('approved', 'rejected')", name="ck_order_approval_decision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    order_intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "connection_id",
            "external_event_id",
            name="uq_execution_event_external",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_execution_event_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "connection_id"),
            (
                "broker_connections.workspace_id",
                "broker_connections.account_id",
                "broker_connections.id",
            ),
            name="fk_execution_event_scope_connection",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_execution_event_scope_trade",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "order_intent_id"),
            (
                "order_intents.workspace_id",
                "order_intents.account_id",
                "order_intents.id",
            ),
            name="fk_execution_event_scope_intent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "connection_id",
            "external_fill_id",
            name="uq_fill_external",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "connection_id"),
            (
                "broker_connections.workspace_id",
                "broker_connections.account_id",
                "broker_connections.id",
            ),
            name="fk_fill_scope_connection",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_fill_scope_trade",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "execution_event_id"),
            (
                "execution_events.workspace_id",
                "execution_events.account_id",
                "execution_events.id",
            ),
            name="fk_fill_scope_execution_event",
        ),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_fill_side"),
        CheckConstraint("quantity > 0", name="ck_fill_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    execution_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
    guaranteed_execution_fee: Mapped[Decimal | None] = mapped_column(MONEY)
    half_spread_cost: Mapped[Decimal | None] = mapped_column(MONEY)
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
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_management_scope_trade",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "order_intent_id"),
            (
                "order_intents.workspace_id",
                "order_intents.account_id",
                "order_intents.id",
            ),
            name="fk_management_scope_intent",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "execution_event_id"),
            (
                "execution_events.workspace_id",
                "execution_events.account_id",
                "execution_events.id",
            ),
            name="fk_management_scope_execution_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    execution_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    quantity_delta: Mapped[Decimal | None] = mapped_column(QUANTITY)
    position_quantity_after: Mapped[Decimal | None] = mapped_column(QUANTITY)
    realized_r_at_event: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    reason: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_position_snapshot_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_position_snapshot_scope_trade",
        ),
        CheckConstraint(
            "trigger IN ('fill', 'management', 'review', 'manual', 'reconciliation')",
            name="ck_position_snapshot_trigger",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_account_snapshot_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "execution_event_id"),
            (
                "execution_events.workspace_id",
                "execution_events.account_id",
                "execution_events.id",
            ),
            name="fk_account_snapshot_scope_execution_event",
        ),
        CheckConstraint(
            "trigger IN ('fill', 'management', 'review', 'manual', 'reconciliation')",
            name="ck_account_snapshot_trigger",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    execution_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
            "workspace_id",
            "account_id",
            "source",
            "instrument_id",
            "timeframe",
            "market_time",
            name="uq_market_context_observation",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_market_context_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_market_context_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_plan_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_market_context_scope_trade_plan",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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


class TradingViewAlert(Base):
    """Verified inbound TradingView alert stored as untrusted market evidence."""

    __tablename__ = "tradingview_alerts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "external_event_id",
            name="uq_tradingview_alert_external_event",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "payload_sha256",
            name="uq_tradingview_alert_payload",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_tradingview_workspace_account",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    external_event_id: Mapped[str] = mapped_column(String(160))
    alert_name: Mapped[str] = mapped_column(String(160))
    symbol: Mapped[str] = mapped_column(String(80), index=True)
    exchange: Mapped[str | None] = mapped_column(String(80), index=True)
    timeframe: Mapped[str] = mapped_column(String(24), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    condition: Mapped[str | None] = mapped_column(String(300))
    market_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    open_price: Mapped[Decimal | None] = mapped_column(PRICE)
    high_price: Mapped[Decimal | None] = mapped_column(PRICE)
    low_price: Mapped[Decimal | None] = mapped_column(PRICE)
    close_price: Mapped[Decimal | None] = mapped_column(PRICE)
    volume: Mapped[Decimal | None] = mapped_column(QUANTITY)
    note: Mapped[str | None] = mapped_column(String(1000))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    verified_source_ip: Mapped[str] = mapped_column(String(45))
    verification_method: Mapped[str] = mapped_column(String(64))


class ChatWebhookMessage(Base):
    """Normalized inbound chat message evidence from Telegram or Discord webhooks."""

    __tablename__ = "chat_webhook_messages"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "platform",
            "external_message_id",
            name="uq_chat_webhook_message_external",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "payload_sha256",
            name="uq_chat_webhook_message_payload",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_chat_webhook_workspace_account",
        ),
        Index(
            "ix_chat_webhook_platform_time",
            "workspace_id",
            "account_id",
            "platform",
            "sent_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)
    external_message_id: Mapped[str] = mapped_column(String(160), index=True)
    sender_id: Mapped[str] = mapped_column(String(120))
    sender_name: Mapped[str | None] = mapped_column(String(160))
    channel_id: Mapped[str | None] = mapped_column(String(120))
    channel_name: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    verified_source: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    payload_sha256: Mapped[str] = mapped_column(String(64))

class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "sha256",
            "storage_uri",
            name="uq_evidence_content_location",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_evidence_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_evidence_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_evidence_scope_trade",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_plan_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_evidence_scope_trade_plan",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "market_context_id"),
            (
                "market_contexts.workspace_id",
                "market_contexts.account_id",
                "market_contexts.id",
            ),
            name="fk_evidence_scope_market_context",
        ),
        CheckConstraint(
            "evidence_type IN ('chart', 'news', 'calendar', 'note', 'broker_record')",
            name="ck_evidence_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    market_context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_analysis_run_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "evidence_id"),
            (
                "evidence_items.workspace_id",
                "evidence_items.account_id",
                "evidence_items.id",
            ),
            name="fk_analysis_run_scope_evidence",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_plan_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_analysis_run_scope_trade_plan",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed', 'corrected')",
            name="ck_analysis_run_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_observation_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_plan_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_observation_scope_trade_plan",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "market_context_id"),
            (
                "market_contexts.workspace_id",
                "market_contexts.account_id",
                "market_contexts.id",
            ),
            name="fk_observation_scope_market_context",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "evidence_id"),
            (
                "evidence_items.workspace_id",
                "evidence_items.account_id",
                "evidence_items.id",
            ),
            name="fk_observation_scope_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    market_context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_trade_reflection_scope_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "trade_id",
            name="uq_trade_reflection_scope_trade",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_trade_reflection_scope_plan",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "lifecycle_trade_id"),
            ("trades.workspace_id", "trades.account_id", "trades.id"),
            name="fk_trade_reflection_scope_trade",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    lifecycle_trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "reflection_id"),
            (
                "trade_reflections.workspace_id",
                "trade_reflections.account_id",
                "trade_reflections.id",
            ),
            name="fk_rule_evaluation_scope_reflection",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_rule_evaluation_workspace_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    reflection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_mindset_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_mindset_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_mindset_workspace_version",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_plan_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_mindset_scope_trade_plan",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    phase: Mapped[str] = mapped_column(String(24))
    readiness: Mapped[int] = mapped_column(Integer)
    accepted_risk: Mapped[bool] = mapped_column(Boolean)
    emotion_tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    emotional_state: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class PretradeAssessment(Base):
    __tablename__ = "pretrade_assessments"
    __table_args__ = (
        CheckConstraint(
            "rating IN ('eligible', 'conditional', 'stand_aside', 'blocked')",
            name="ck_pretrade_assessment_rating",
        ),
        CheckConstraint(
            "human_decision IN ('pending', 'proceed', 'stand_aside', 'cancelled')",
            name="ck_pretrade_assessment_decision",
        ),
        CheckConstraint(
            "news_status IN ('fresh', 'stale', 'not_configured', 'unavailable')",
            name="ck_pretrade_assessment_news_status",
        ),
        CheckConstraint(
            "human_decision != 'proceed' "
            "OR (rating IN ('eligible', 'conditional') AND trade_plan_id IS NOT NULL)",
            name="ck_pretrade_assessment_proceed_eligible",
        ),
        CheckConstraint(
            "human_decision = 'proceed' OR trade_plan_id IS NULL",
            name="ck_pretrade_assessment_trade_decision",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_pretrade_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_pretrade_workspace_version",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "mindset_checkin_id"),
            (
                "mindset_checkins.workspace_id",
                "mindset_checkins.account_id",
                "mindset_checkins.id",
            ),
            name="fk_pretrade_scope_mindset",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "account_constraint_profile_id"),
            (
                "account_constraint_profiles.workspace_id",
                "account_constraint_profiles.trading_account_id",
                "account_constraint_profiles.id",
            ),
            name="fk_pretrade_scope_constraint",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "trade_plan_id"),
            ("trade_plans.workspace_id", "trade_plans.account_id", "trade_plans.id"),
            name="fk_pretrade_scope_trade_plan",
        ),
        Index(
            "ix_pretrade_scope_recall",
            "workspace_id",
            "account_id",
            "playbook_version_id",
            "setup_key",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    mindset_checkin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    account_constraint_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    trade_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    setup_key: Mapped[str | None] = mapped_column(String(120))
    rating: Mapped[str] = mapped_column(String(24), index=True)
    component_scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    hard_blockers: Mapped[list[str]] = mapped_column(JSONB, default=list)
    stand_aside_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JSONB, default=list)
    rule_results: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    news_status: Mapped[str] = mapped_column(String(24))
    market_context: Mapped[dict] = mapped_column(JSONB, default=dict)
    policy_hash: Mapped[str] = mapped_column(String(64))
    human_decision: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "name",
            name="uq_conversation_workspace_account_name",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_conversation_scope_id",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_conversation_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "active_playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_conversation_workspace_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(160), default="Trading Agent session")
    active_playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
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
        CheckConstraint(
            "status IN ('pending', 'complete', 'partial', 'failed')",
            name="ck_turn_status",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "id",
            name="uq_conversation_turn_scope_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "session_id",
            "request_id",
            "role",
            name="uq_conversation_turn_request_role",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_turn_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "session_id"),
            (
                "conversation_sessions.workspace_id",
                "conversation_sessions.account_id",
                "conversation_sessions.id",
            ),
            name="fk_turn_scope_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_turn_workspace_version",
        ),
        Index(
            "ix_turn_scope_history",
            "workspace_id",
            "account_id",
            "session_id",
            "playbook_version_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), default="complete", index=True)
    error_type: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    session: Mapped[ConversationSession] = relationship(back_populates="turns")


class ToolExecutionAudit(Base):
    """Durable mutation-tool lifecycle and request-scoped idempotency record."""

    __tablename__ = "tool_execution_audits"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "request_id",
            "tool_name",
            "arguments_hash",
            name="uq_tool_execution_request_arguments",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'declined', 'succeeded', 'failed')",
            name="ck_tool_execution_status",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id"),
            ("trading_accounts.workspace_id", "trading_accounts.id"),
            name="fk_tool_execution_workspace_account",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "conversation_session_id"),
            (
                "conversation_sessions.workspace_id",
                "conversation_sessions.account_id",
                "conversation_sessions.id",
            ),
            name="fk_tool_execution_scope_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "account_id", "user_turn_id"),
            (
                "conversation_turns.workspace_id",
                "conversation_turns.account_id",
                "conversation_turns.id",
            ),
            name="fk_tool_execution_scope_turn",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("workspace_id", "playbook_version_id"),
            ("playbook_versions.workspace_id", "playbook_versions.id"),
            name="fk_tool_execution_workspace_version",
        ),
        Index(
            "ix_tool_execution_scope_request",
            "workspace_id",
            "account_id",
            "request_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    conversation_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    user_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    playbook_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        index=True,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    tool_name: Mapped[str] = mapped_column(String(120))
    arguments_hash: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result_text: Mapped[str | None] = mapped_column(Text)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    failure_type: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
