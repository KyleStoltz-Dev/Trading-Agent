import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Playbook, PlaybookVersion
from app.services.catalog import create_playbook_version
from app.services.pretrade import strategy_rules
from app.services.workspaces import RequestScope, validate_scope

RuleText = Annotated[str, Field(min_length=3, max_length=500)]
ShortText = Annotated[str, Field(min_length=1, max_length=160)]


def _normalize_text(value: str) -> str:
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        and not character.isspace()
        for character in value
    ):
        raise ValueError("strategy text cannot contain control or directionality characters")
    normalized = " ".join(value.split())
    unsafe_patterns = (
        r"https?://",
        r"\bignore\s+(?:all\s+)?(?:previous|prior|system)\b",
        r"\bsystem\s+prompt\b",
        r"\b(?:call|invoke|execute)\s+(?:a\s+)?tool\b",
        r"\b(?:api[_ -]?key|access[_ -]?token|password|secret)\b",
    )
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in unsafe_patterns):
        raise ValueError(
            "strategy text cannot contain URLs, credentials, or model-control instructions"
        )
    return normalized


def _normalized_unique(values: object, *, field: str):
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        return values
    normalized = [_normalize_text(value) for value in values]
    if len({value.casefold() for value in normalized}) != len(normalized):
        raise ValueError(f"{field} cannot contain duplicate values")
    return normalized


class StrictDefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrategyContextV1(StrictDefinitionModel):
    required: list[RuleText] = Field(default_factory=list, max_length=100)
    exclusions: list[RuleText] = Field(default_factory=list, max_length=100)

    @field_validator("required", "exclusions", mode="before")
    @classmethod
    def normalize_rules(cls, value: list[str], info) -> list[str]:
        return _normalized_unique(value, field=info.field_name)


class StrategySetupV1(StrictDefinitionModel):
    key: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    requirements: list[RuleText] = Field(default_factory=list, max_length=100)
    exclusions: list[RuleText] = Field(default_factory=list, max_length=100)

    @field_validator("key", mode="before")
    @classmethod
    def normalize_key(cls, value):
        if not isinstance(value, str):
            return value
        return re.sub(r"[-_]+", "_", value.strip().casefold())

    @field_validator("requirements", "exclusions", mode="before")
    @classmethod
    def normalize_rules(cls, value: list[str], info) -> list[str]:
        return _normalized_unique(value, field=info.field_name)

    @model_validator(mode="after")
    def require_rules(self) -> "StrategySetupV1":
        if not self.requirements and not self.exclusions:
            raise ValueError("each setup requires at least one requirement or exclusion")
        return self


class StrategyCompositionV1(StrictDefinitionModel):
    wyckoff_role: str | None = Field(default=None, min_length=3, max_length=1000)
    ict_role: str | None = Field(default=None, min_length=3, max_length=1000)
    conflict_rule: str | None = Field(default=None, min_length=3, max_length=500)

    @field_validator(
        "wyckoff_role",
        "ict_role",
        "conflict_rule",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value):
        return _normalize_text(value) if isinstance(value, str) and value else value


class StrategyRiskV1(StrictDefinitionModel):
    maximum_risk_percent: Decimal | None = Field(default=None, gt=0, le=5)
    minimum_planned_r: Decimal | None = Field(default=None, gt=0, le=100)
    human_confirms_every_trade: Literal[True] = True


class StrategyMindsetV1(StrictDefinitionModel):
    caution_emotion_tags: list[ShortText] = Field(
        default_factory=list,
        max_length=20,
    )

    @field_validator("caution_emotion_tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        return _normalized_unique(value, field="caution_emotion_tags")


class StrategyDefinitionV1(StrictDefinitionModel):
    methodology: str = Field(min_length=2, max_length=160)
    objective: str = Field(min_length=3, max_length=1000)
    composition: StrategyCompositionV1 | None = None
    requirements: list[RuleText] = Field(default_factory=list, max_length=100)
    exclusions: list[RuleText] = Field(default_factory=list, max_length=100)
    context: StrategyContextV1 = Field(default_factory=StrategyContextV1)
    setups: list[StrategySetupV1] = Field(default_factory=list, max_length=20)
    allowed_vocabulary: list[ShortText] = Field(default_factory=list, max_length=100)
    forbidden_cross_strategy_concepts: list[ShortText] = Field(
        default_factory=list,
        max_length=100,
    )
    mindset: StrategyMindsetV1 = Field(default_factory=StrategyMindsetV1)
    risk: StrategyRiskV1 = Field(default_factory=StrategyRiskV1)

    @field_validator("methodology", "objective", mode="before")
    @classmethod
    def normalize_text(cls, value):
        return _normalize_text(value) if isinstance(value, str) else value

    @field_validator(
        "requirements",
        "exclusions",
        "allowed_vocabulary",
        "forbidden_cross_strategy_concepts",
        mode="before",
    )
    @classmethod
    def normalize_lists(cls, value: list[str], info) -> list[str]:
        return _normalized_unique(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_strategy(self) -> "StrategyDefinitionV1":
        keys = [setup.key.casefold() for setup in self.setups]
        if len(set(keys)) != len(keys):
            raise ValueError("setup keys must be unique")
        canonical = self.model_dump(mode="json", exclude_none=True)
        if self.setups:
            for setup in self.setups:
                strategy_rules(canonical, setup_key=setup.key)
        else:
            strategy_rules(canonical)
        return self


def canonical_strategy_definition(
    definition: dict,
    *,
    maximum_risk_percent: Decimal,
) -> dict:
    parsed = StrategyDefinitionV1.model_validate(definition)
    strategy_maximum = parsed.risk.maximum_risk_percent
    if (
        strategy_maximum is not None
        and strategy_maximum > maximum_risk_percent
    ):
        raise ValueError(
            f"strategy maximum risk {strategy_maximum}% exceeds the application "
            f"maximum {maximum_risk_percent}%"
        )
    return parsed.model_dump(mode="json", exclude_none=True)


def strategy_proposal_hash(proposal: dict) -> str:
    serialized = json.dumps(
        proposal,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def create_validated_strategy_version(
    db: Session,
    *,
    scope: RequestScope,
    name: str,
    definition: dict,
    maximum_risk_percent: Decimal,
    description: str = "",
    change_hypothesis: str | None = None,
    sample_requirement: int | None = None,
    created_by: str = "human",
) -> PlaybookVersion:
    validate_scope(db, scope)
    normalized_name = " ".join(name.split())
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,119}",
        normalized_name,
    ):
        raise ValueError(
            "strategy name must be 2-120 letters, numbers, spaces, dots, "
            "underscores, or hyphens"
        )
    canonical = canonical_strategy_definition(
        definition,
        maximum_risk_percent=maximum_risk_percent,
    )
    existing = db.scalar(
        select(Playbook).where(
            Playbook.workspace_id == scope.workspace_id,
            func.lower(Playbook.name) == normalized_name.casefold()
        )
    )
    if existing is not None:
        normalized_name = existing.name
        if not change_hypothesis:
            raise ValueError(
                "a new version of an existing strategy requires a change hypothesis"
            )
    return create_playbook_version(
        db,
        workspace_id=scope.workspace_id,
        name=normalized_name,
        definition=canonical,
        description=" ".join(description.split()),
        change_hypothesis=(
            " ".join(change_hypothesis.split()) if change_hypothesis else None
        ),
        sample_requirement=sample_requirement,
        created_by=created_by,
    )
