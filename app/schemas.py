import re
import uuid
from datetime import datetime
from decimal import Decimal
from math import isfinite
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from app.services.profile_validation import (
    validate_profile_text,
    validate_reflective_text,
)

Direction = Literal["long", "short"]
TradingViewMetadataValue = str | int | float | bool | None


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    active: bool
    created_at: datetime


class TradingAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    broker: str
    external_account_id: str
    label: str
    currency: str
    mode: Literal["practice", "live", "backtest"]
    active: bool
    is_default: bool
    created_at: datetime


class TradingViewAlertCreate(BaseModel):
    """Strict TradingView payload; every text field remains untrusted evidence."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        min_length=8,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$",
    )
    alert_name: str = Field(min_length=1, max_length=160)
    symbol: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    exchange: str | None = Field(
        default=None,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    timeframe: str = Field(
        min_length=1,
        max_length=24,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    event_type: str = Field(
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    condition: str | None = Field(default=None, max_length=300)
    market_time: datetime
    open: Decimal | None = Field(default=None, allow_inf_nan=False)
    high: Decimal | None = Field(default=None, allow_inf_nan=False)
    low: Decimal | None = Field(default=None, allow_inf_nan=False)
    close: Decimal | None = Field(default=None, allow_inf_nan=False)
    volume: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, TradingViewMetadataValue] = Field(default_factory=dict)

    @field_validator(
        "alert_name",
        "symbol",
        "timeframe",
    )
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("control characters are not allowed")
        return normalized

    @field_validator("exchange", "condition", "note")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if any(
            ord(character) < 32 and character not in "\t\n"
            for character in normalized
        ):
            raise ValueError("control characters are not allowed")
        return normalized

    @field_validator("symbol", "exchange")
    @classmethod
    def normalize_market_identifier(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @field_validator("timeframe")
    @classmethod
    def normalize_timeframe(cls, value: str) -> str:
        return value.upper()

    @field_validator("market_time")
    @classmethod
    def require_aware_trigger_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market_time must include a timezone")
        return value

    @field_validator("metadata")
    @classmethod
    def bound_metadata(
        cls,
        value: dict[str, TradingViewMetadataValue],
    ) -> dict[str, TradingViewMetadataValue]:
        if len(value) > 20:
            raise ValueError("metadata supports at most 20 fields")
        for key, item in value.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key):
                raise ValueError("metadata keys must be short identifiers")
            if isinstance(item, str) and len(item) > 300:
                raise ValueError("metadata text values cannot exceed 300 characters")
            if isinstance(item, float) and not isfinite(item):
                raise ValueError("metadata numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_price_bar(self) -> "TradingViewAlertCreate":
        prices = (self.open, self.high, self.low, self.close)
        populated = [price is not None for price in prices]
        if any(populated) and not all(populated):
            raise ValueError("open, high, low, and close must be supplied together")
        if all(populated):
            open_price = self.open
            high_price = self.high
            low_price = self.low
            close_price = self.close
            if (
                open_price is None
                or high_price is None
                or low_price is None
                or close_price is None
            ):
                raise ValueError("price bar validation failed")
            if high_price < low_price:
                raise ValueError("high cannot be below low")
            if not low_price <= open_price <= high_price:
                raise ValueError("open must be within the high-low range")
            if not low_price <= close_price <= high_price:
                raise ValueError("close must be within the high-low range")
        return self


class TradingViewAlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_event_id: str
    alert_name: str
    symbol: str
    exchange: str | None
    timeframe: str
    event_type: str
    condition: str | None
    market_time: datetime
    received_at: datetime
    open_price: Decimal | None
    high_price: Decimal | None
    low_price: Decimal | None
    close_price: Decimal | None
    volume: Decimal | None
    note: str | None
    metadata_json: dict
    payload_sha256: str
    verification_method: str


class TradingViewWebhookCreate(TradingViewAlertCreate):
    """Inbound envelope; the account secret is validated and never persisted."""

    webhook_secret: SecretStr = Field(
        min_length=32,
        max_length=200,
        exclude=True,
    )
    sent_at: datetime = Field(
        exclude=True,
        description="TradingView alert fire time supplied with {{timenow}}.",
    )

    @field_validator("sent_at")
    @classmethod
    def sent_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sent_at must include a timezone")
        return value

    def alert(self) -> TradingViewAlertCreate:
        return TradingViewAlertCreate.model_validate(
            self.model_dump(exclude={"webhook_secret", "sent_at"})
        )


class TradingViewWebhookReceipt(BaseModel):
    accepted: bool
    duplicate: bool
    alert_id: uuid.UUID
    event_id: str


class PositionSizeRequest(BaseModel):
    account_equity: Decimal = Field(gt=0)
    risk_percent: Decimal = Field(gt=0, le=5)
    entry: Decimal
    stop: Decimal
    target: Decimal | None = None
    value_per_price_unit: Decimal = Field(
        gt=0,
        description="PnL change for one unit of position when price moves by one.",
    )

    @model_validator(mode="after")
    def validate_prices(self) -> "PositionSizeRequest":
        if self.entry == self.stop:
            raise ValueError("entry and stop must differ")
        return self


class PositionSizeResult(BaseModel):
    risk_amount: Decimal
    stop_distance: Decimal
    quantity: Decimal
    planned_r: Decimal | None


class InstrumentSpecificationCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    external_symbol: str = Field(min_length=1, max_length=80)
    venue: str | None = Field(default=None, max_length=80)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    asset_class: str = Field(default="unknown", min_length=1, max_length=32)
    contract_size: Decimal = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    tick_value_per_quantity_unit: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    maximum_quantity: Decimal = Field(gt=0)
    quantity_step: Decimal = Field(gt=0)
    margin_rate: Decimal | None = Field(default=None, gt=0, le=1)
    estimated_spread: Decimal | None = Field(default=None, ge=0)
    commission_per_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    financing_per_quantity_day: Decimal | None = None
    pnl_currency: str = Field(min_length=3, max_length=12)
    source: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_quantity_range(self) -> "InstrumentSpecificationCreate":
        if self.maximum_quantity < self.minimum_quantity:
            raise ValueError("maximum_quantity must be at least minimum_quantity")
        return self


class BrokerPositionSizeRequest(BaseModel):
    account_equity: Decimal = Field(gt=0)
    available_margin: Decimal | None = Field(default=None, ge=0)
    risk_percent: Decimal = Field(gt=0, le=5)
    entry: Decimal
    stop: Decimal
    target: Decimal | None = None
    conversion_rate_to_account: Decimal = Field(default=Decimal("1"), gt=0)
    estimated_slippage: Decimal = Field(default=Decimal("0"), ge=0)
    maximum_risk_percent: Decimal = Field(default=Decimal("1"), gt=0, le=5)

    @model_validator(mode="after")
    def validate_broker_risk(self) -> "BrokerPositionSizeRequest":
        if self.entry == self.stop:
            raise ValueError("entry and stop must differ")
        if self.risk_percent > self.maximum_risk_percent:
            raise ValueError("requested risk exceeds the configured maximum")
        return self


class BrokerPositionSizeResult(BaseModel):
    quantity: Decimal
    risk_budget: Decimal
    estimated_loss_at_stop: Decimal
    estimated_costs: Decimal
    estimated_margin: Decimal | None
    stop_ticks: Decimal
    planned_r: Decimal | None
    limited_by: str | None


class TradePlanCreate(PositionSizeRequest):
    instrument: str = Field(min_length=1, max_length=32)
    venue: str | None = Field(default=None, max_length=64)
    direction: Direction
    setup_name: str = Field(min_length=1, max_length=120)
    regime: str | None = Field(default=None, max_length=64)
    session_name: str | None = Field(default=None, max_length=40)
    market_time: datetime | None = None
    context_timeframe: str = Field(min_length=1, max_length=16)
    trigger_timeframe: str = Field(min_length=1, max_length=16)
    thesis: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    observations: list[str] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list)
    sizing_provider: str | None = Field(default=None, max_length=40)
    sizing_symbol: str | None = Field(default=None, max_length=80)
    available_margin: Decimal | None = Field(default=None, ge=0)
    conversion_rate_to_account: Decimal = Field(default=Decimal("1"), gt=0)
    estimated_slippage: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def validate_direction(self) -> "TradePlanCreate":
        if self.market_time is not None and (
            self.market_time.tzinfo is None or self.market_time.utcoffset() is None
        ):
            raise ValueError("market_time must be timezone-aware")
        if self.direction == "long" and self.stop >= self.entry:
            raise ValueError("a long stop must be below entry")
        if self.direction == "short" and self.stop <= self.entry:
            raise ValueError("a short stop must be above entry")
        if self.target is None:
            raise ValueError("target is required for a trade plan")
        if self.direction == "long" and self.target <= self.entry:
            raise ValueError("a long target must be above entry")
        if self.direction == "short" and self.target >= self.entry:
            raise ValueError("a short target must be below entry")
        if bool(self.sizing_provider) != bool(self.sizing_symbol):
            raise ValueError("sizing_provider and sizing_symbol must be configured together")
        return self


class TradePlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    reference: str
    playbook_version_id: uuid.UUID | None
    instrument: str
    venue: str | None
    direction: str
    setup_name: str
    regime: str | None
    session_name: str | None
    market_time: datetime | None = Field(validation_alias="source_time")
    minutes_to_high_impact_event: int | None
    context_timeframe: str
    trigger_timeframe: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    account_equity: Decimal
    risk_percent: Decimal
    value_per_price_unit: Decimal
    risk_amount: Decimal
    estimated_costs: Decimal | None
    estimated_margin: Decimal | None
    quantity: Decimal
    planned_r: Decimal
    thesis: str
    invalidation: str
    observations: list[str]
    interpretations: list[str]
    status: str
    created_at: datetime


class ReflectionCreate(BaseModel):
    exit_average: Decimal
    realized_pnl: Decimal
    execution_grade: Literal["A", "B", "C", "D", "F"]
    rule_adherence: list[dict] = Field(default_factory=list)
    emotion_before: str | None = None
    emotion_during: str | None = None
    emotion_after: str | None = None
    process_score: Decimal | None = Field(default=None, ge=0, le=100)
    outcome_score: Decimal | None = Field(default=None, ge=0, le=100)
    maximum_favorable_excursion_r: Decimal | None = None
    maximum_adverse_excursion_r: Decimal | None = None
    total_fees: Decimal | None = Field(default=None, ge=0)
    slippage_cost: Decimal | None = Field(default=None, ge=0)
    notes: str = ""


class ReflectionRead(ReflectionCreate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    trade_id: uuid.UUID
    realized_r: Decimal
    created_at: datetime


MindsetPhase = Literal["pre_session", "pre_trade", "during_trade", "post_trade"]


class MindsetCheckInCreate(BaseModel):
    phase: MindsetPhase
    readiness: int = Field(ge=1, le=5)
    accepted_risk: bool
    emotion_tags: list[str] = Field(default_factory=list, max_length=20)
    emotional_state: str | None = Field(default=None, max_length=2_000)
    note: str | None = Field(default=None, max_length=2_000)
    trade_reference: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def normalize_tags(self) -> "MindsetCheckInCreate":
        normalized = list(
            dict.fromkeys(tag.strip().lower() for tag in self.emotion_tags if tag.strip())
        )
        if any(len(tag) > 40 for tag in normalized):
            raise ValueError("emotion tags must be at most 40 characters")
        self.emotion_tags = normalized
        if self.emotional_state is not None:
            self.emotional_state = validate_reflective_text(
                self.emotional_state,
                field_name="emotional state",
            )
        if self.note is not None:
            stripped_note = self.note.strip()
            self.note = (
                validate_reflective_text(
                    stripped_note,
                    field_name="process note",
                )
                if stripped_note
                else None
            )
        if self.trade_reference is not None:
            self.trade_reference = self.trade_reference.strip()
        return self


class MindsetCheckInRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    playbook_version_id: uuid.UUID | None
    trade_plan_id: uuid.UUID | None
    trade_reference: str | None
    phase: MindsetPhase
    readiness: int
    accepted_risk: bool
    emotion_tags: list[str]
    emotional_state: str | None = None
    note: str | None
    created_at: datetime


class ManagementEventCreate(BaseModel):
    event_type: Literal[
        "partial_taken",
        "stop_moved",
        "target_moved",
        "breakeven_set",
        "runner_left",
        "hedge_considered",
        "hedge_taken",
        "manual_close",
        "note",
    ]
    price: Decimal | None = None
    quantity_delta: Decimal | None = None
    position_quantity_after: Decimal | None = None
    realized_r_at_event: Decimal | None = None
    reason: str = Field(min_length=1)
    occurred_at: datetime
    actor_type: Literal["human", "agent", "import", "system"] = "human"


class EdgeSegment(BaseModel):
    setup_name: str
    instrument: str
    regime: str | None
    session_name: str | None
    playbook_version_id: uuid.UUID | None
    news_proximity_bucket: str
    context_timeframe: str
    trigger_timeframe: str
    sample_size: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal
    expectancy_r: Decimal
    average_win_r: Decimal | None
    average_loss_r: Decimal | None
    process_score_average: Decimal | None
    validated_sample: bool


class EdgeReport(BaseModel):
    minimum_sample: int
    total_reviewed: int
    segments: list[EdgeSegment]


class ChartAnalysis(BaseModel):
    visible_facts: list[str]
    unreadable_or_missing: list[str]
    context_hypotheses: list[str]
    trigger_hypotheses: list[str]
    playbook_checks: list["PlaybookCheck"]
    risk_questions: list[str]
    management_questions: list[str]
    disclaimer: str


class PlaybookCheck(BaseModel):
    check: str
    status: Literal["met", "not_met", "unclear"]
    evidence: list[str]


AccountRulePolicy = Literal["allowed", "prohibited", "restricted", "unknown"]


class AccountRuleLimits(BaseModel):
    maximum_daily_loss_percent: Decimal | None = Field(default=None, gt=0, le=100)
    maximum_total_loss_percent: Decimal | None = Field(default=None, gt=0, le=100)
    profit_target_percent: Decimal | None = Field(default=None, gt=0, le=100)
    minimum_trading_days: int | None = Field(default=None, ge=0, le=365)
    maximum_trading_days: int | None = Field(default=None, ge=1, le=3_650)
    consistency_limit_percent: Decimal | None = Field(default=None, gt=0, le=100)
    drawdown_type: Literal[
        "static",
        "balance_based",
        "equity_based",
        "trailing",
        "unknown",
    ] = "unknown"
    news_trading: AccountRulePolicy = "unknown"
    overnight_holding: AccountRulePolicy = "unknown"
    weekend_holding: AccountRulePolicy = "unknown"
    daily_reset_timezone: str | None = Field(default=None, max_length=80)
    custom_rules: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("daily_reset_timezone")
    @classmethod
    def validate_daily_reset_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("daily reset timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("custom_rules")
    @classmethod
    def validate_custom_rules(cls, values: list[str]) -> list[str]:
        normalized = [
            validate_profile_text(
                value,
                field_name="account rule",
                maximum_length=240,
            )
            for value in values
        ]
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("account rules cannot contain duplicates")
        if sum(len(value) for value in normalized) > 3_000:
            raise ValueError("account rules exceed the combined 3,000-character limit")
        return normalized

    @model_validator(mode="after")
    def validate_day_range(self) -> "AccountRuleLimits":
        if (
            self.minimum_trading_days is not None
            and self.maximum_trading_days is not None
            and self.maximum_trading_days < self.minimum_trading_days
        ):
            raise ValueError(
                "maximum trading days must be at least minimum trading days"
            )
        return self


class AccountConstraintUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    account_type: Literal["personal", "prop"]
    account_size: Decimal = Field(gt=0, le=Decimal("1000000000000"))
    currency: str = Field(min_length=3, max_length=12)
    firm_name: str | None = Field(default=None, max_length=120)
    program_name: str | None = Field(default=None, max_length=120)
    phase: Literal["personal", "evaluation", "verification", "funded"]
    rules: AccountRuleLimits = Field(default_factory=AccountRuleLimits)

    @field_validator("name", "firm_name", "program_name")
    @classmethod
    def validate_account_labels(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return validate_profile_text(
            value,
            field_name=info.field_name.replace("_", " "),
            maximum_length=120,
        )

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", normalized):
            raise ValueError("currency must look like USD, EUR, or USDT")
        return normalized

    @model_validator(mode="after")
    def validate_account_type(self) -> "AccountConstraintUpsert":
        if self.account_type == "personal":
            if self.phase != "personal":
                raise ValueError("a personal account must use the personal phase")
            if self.firm_name is not None or self.program_name is not None:
                raise ValueError(
                    "prop firm and program names apply only to prop accounts"
                )
        else:
            if self.phase == "personal":
                raise ValueError("a prop account must select its program phase")
            if self.firm_name is None:
                raise ValueError("a prop account requires the prop firm name")
        return self


class AccountConstraintRead(AccountConstraintUpsert):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    trading_account_id: uuid.UUID
    profile_id: uuid.UUID
    active: bool
    created_at: datetime
    updated_at: datetime


class TraderProfileUpsert(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    experience_level: Literal["beginner", "intermediate", "advanced"] | None = None
    trading_style: str = Field(default="", max_length=4_000)
    markets: list[str] = Field(default_factory=list, max_length=100)
    sessions: list[str] = Field(default_factory=list, max_length=12)
    goals: list[str] = Field(default_factory=list, max_length=20)
    risk_preferences: dict = Field(default_factory=dict)
    onboarding_complete: bool = True

    @field_validator("display_name", "trading_style", "timezone")
    @classmethod
    def validate_profile_fields(cls, value: str, info) -> str:
        trading_style = info.field_name == "trading_style"
        return validate_profile_text(
            value,
            field_name=info.field_name.replace("_", " "),
            allow_empty=trading_style,
            maximum_length=(
                4_000 if trading_style else 80 if info.field_name == "timezone" else 120
            ),
        )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("markets")
    @classmethod
    def validate_markets(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not re.fullmatch(r"[A-Z0-9._:/-]{2,32}", value) for value in normalized):
            raise ValueError("markets must contain broker-style symbols")
        if len(set(normalized)) != len(normalized):
            raise ValueError("markets cannot contain duplicates")
        return normalized

    @field_validator("sessions")
    @classmethod
    def validate_sessions(cls, values: list[str]) -> list[str]:
        normalized = [
            validate_profile_text(
                value,
                field_name="session",
                maximum_length=48,
            )
            for value in values
        ]
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("sessions cannot contain duplicates")
        if sum(len(value) for value in normalized) > 384:
            raise ValueError("sessions exceed the combined 384-character limit")
        return normalized

    @field_validator("goals")
    @classmethod
    def validate_goals(cls, values: list[str]) -> list[str]:
        normalized = [
            validate_profile_text(
                value,
                field_name="goal",
                require_trading_goal=True,
                maximum_length=160,
            )
            for value in values
        ]
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("goals cannot contain duplicates")
        if sum(len(value) for value in normalized) > 2_000:
            raise ValueError("goals exceed the combined 2,000-character limit")
        return normalized

    @field_validator("risk_preferences")
    @classmethod
    def validate_risk_preferences(cls, value: dict) -> dict:
        unknown = set(value) - {"maximum_trade_risk_percent"}
        if unknown:
            raise ValueError("unsupported risk preference fields are not allowed")
        configured = value.get("maximum_trade_risk_percent")
        if configured is not None:
            try:
                maximum = Decimal(str(configured))
            except Exception as exc:
                raise ValueError("maximum_trade_risk_percent must be a number") from exc
            if not maximum.is_finite() or maximum <= 0 or maximum > 5:
                raise ValueError("maximum_trade_risk_percent must be greater than 0 and at most 5")
        return value


class TraderProfileRead(TraderProfileUpsert):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    profile_key: str
    created_at: datetime
    updated_at: datetime


class StrategySummary(BaseModel):
    playbook_id: uuid.UUID
    playbook_version_id: uuid.UUID
    name: str
    description: str
    version: int
    content_hash: str
    sample_requirement: int | None
    knowledge_items: int = 0


class KnowledgeItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    playbook_version_id: uuid.UUID
    kind: str
    source_reference: str | None
    author: str | None
    occurred_at: datetime | None
    content: str
    content_hash: str
    excluded: bool
    created_at: datetime


class KnowledgeImportResult(BaseModel):
    import_id: uuid.UUID
    strategy: str
    strategy_version: int
    source_name: str
    source_type: str
    imported: int
    skipped: int


class StrategyExperimentCreate(BaseModel):
    strategy: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    mode: Literal["backtest", "forward_test"]
    hypothesis: str = Field(min_length=1)
    instrument: str | None = Field(default=None, max_length=40)
    timeframe: str | None = Field(default=None, max_length=16)
    data_start: datetime | None = None
    data_end: datetime | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> "StrategyExperimentCreate":
        for value in (self.data_start, self.data_end):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("experiment dates must be timezone-aware")
        if (
            self.data_start is not None
            and self.data_end is not None
            and self.data_start > self.data_end
        ):
            raise ValueError("data_start must not be after data_end")
        return self


class StrategyExperimentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    playbook_version_id: uuid.UUID
    name: str
    mode: str
    status: str
    hypothesis: str
    instrument: str | None
    timeframe: str | None
    data_start: datetime | None
    data_end: datetime | None
    rules_hash: str
    created_at: datetime
    completed_at: datetime | None


class StrategyTestSampleCreate(BaseModel):
    occurred_at: datetime
    instrument: str = Field(min_length=1, max_length=40)
    setup_key: str = Field(min_length=1, max_length=120)
    classification: Literal["eligible", "excluded", "unclear"]
    exclusion_reason: str | None = None
    outcome_r: Decimal | None = None
    process_score: Decimal | None = Field(default=None, ge=0, le=100)
    feature_snapshot: dict = Field(default_factory=dict)
    notes: str = ""
    source_reference: str | None = None

    @model_validator(mode="after")
    def validate_sample(self) -> "StrategyTestSampleCreate":
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.classification == "excluded" and not self.exclusion_reason:
            raise ValueError("excluded samples require an exclusion_reason")
        return self
