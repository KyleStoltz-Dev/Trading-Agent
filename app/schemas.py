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


class TradePlanCreate(PositionSizeRequest):
    instrument: str = Field(min_length=1, max_length=32)
    venue: str | None = Field(default=None, max_length=64)
    direction: Direction
    setup_name: str = Field(min_length=1, max_length=120)
    regime: str | None = Field(default=None, max_length=64)
    context_timeframe: str = Field(min_length=1, max_length=16)
    trigger_timeframe: str = Field(min_length=1, max_length=16)
    thesis: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    observations: list[str] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_direction(self) -> "TradePlanCreate":
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
        return self


class TradePlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    instrument: str
    venue: str | None
    direction: str
    setup_name: str
    regime: str | None
    context_timeframe: str
    trigger_timeframe: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    account_equity: Decimal
    risk_percent: Decimal
    value_per_price_unit: Decimal
    risk_amount: Decimal
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
    notes: str = ""


class ReflectionRead(ReflectionCreate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trade_id: uuid.UUID
    realized_r: Decimal
    created_at: datetime


class ChartAnalysis(BaseModel):
    visible_facts: list[str]
    unreadable_or_missing: list[str]
    context_hypotheses: list[str]
    trigger_hypotheses: list[str]
    playbook_checks: list[dict]
    risk_questions: list[str]
    management_questions: list[str]
    disclaimer: str

