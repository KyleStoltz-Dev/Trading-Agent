import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Direction = Literal["long", "short"]


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
            raise ValueError(
                "sizing_provider and sizing_symbol must be configured together"
            )
        return self


class TradePlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
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
    trade_id: uuid.UUID
    realized_r: Decimal
    created_at: datetime


MindsetPhase = Literal["pre_session", "pre_trade", "during_trade", "post_trade"]


class MindsetCheckInCreate(BaseModel):
    phase: MindsetPhase
    readiness: int = Field(ge=1, le=5)
    accepted_risk: bool
    emotion_tags: list[str] = Field(default_factory=list, max_length=20)
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
        if self.note is not None:
            self.note = self.note.strip() or None
        if self.trade_reference is not None:
            self.trade_reference = self.trade_reference.strip()
        return self


class MindsetCheckInRead(BaseModel):
    id: uuid.UUID
    playbook_version_id: uuid.UUID | None
    trade_plan_id: uuid.UUID | None
    trade_reference: str | None
    phase: MindsetPhase
    readiness: int
    accepted_risk: bool
    emotion_tags: list[str]
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


class TraderProfileUpsert(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    experience_level: str | None = Field(default=None, max_length=40)
    trading_style: str = Field(default="", max_length=10_000)
    markets: list[str] = Field(default_factory=list, max_length=100)
    sessions: list[str] = Field(default_factory=list, max_length=30)
    goals: list[str] = Field(default_factory=list, max_length=100)
    risk_preferences: dict = Field(default_factory=dict)
    onboarding_complete: bool = True


class TraderProfileRead(TraderProfileUpsert):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
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
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
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
