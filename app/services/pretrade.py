import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.connectors import create_news_connector
from app.models import (
    EconomicEvent,
    MindsetCheckIn,
    Playbook,
    PlaybookVersion,
    PretradeAssessment,
    TradePlan,
)
from app.schemas import MindsetCheckInCreate, MindsetCheckInRead, TradePlanCreate
from app.services.event_relevance import (
    instrument_event_currencies as instrument_event_currencies,
)
from app.services.journal import create_trade_plan
from app.services.mindset import create_mindset_check_in
from app.services.news import store_calendar_events

TRADE_INTENT = re.compile(
    r"\b("
    r"trade|trading|entry|enter|long|short|buy|sell|setup|position|"
    r"premarket|pre-market|outlook|bias|plan"
    r")\b",
    re.IGNORECASE,
)

PREFLIGHT_INTENT_PATTERNS = (
    re.compile(
        r"\bshould\s+(?:i|we)\s+(?:take|enter|open)\s+"
        r"(?:this|the|a|an)?\s*(?:trade|long|short|position)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:review|check|assess|rate)\s+(?:this|my|the)\s+"
        r"(?:trade|setup|entry|long|short|position)\s+"
        r"(?:before|prior\s+to)\s+"
        r"(?:entry|entering|execution|taking\s+it|i\s+enter)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s+am|i['’]?m|im)\s+(?:thinking\s+about|considering)\s+"
        r"(?:taking|entering|opening|buying|selling)\s+"
        r"(?:this|the|a|an)?\s*(?:trade|long|short|position)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s+want|i\s+plan|i['’]?m\s+about|i\s+am\s+about)\s+to\s+"
        r"(?:take|enter|open)\s+(?:this|the|a|an)?\s*"
        r"(?:trade|long|short|position)\b",
        re.IGNORECASE,
    ),
)
NON_PREFLIGHT_CONTEXT = re.compile(
    r"\b("
    r"backtest(?:ing)?|journal(?:ing)?|post[- ]trade|last\s+trade|"
    r"previous\s+trade|trade\s+history|strategy\s+design|hypothetical|"
    r"example\s+trade"
    r")\b",
    re.IGNORECASE,
)


def detect_preflight_intent(message: str) -> bool:
    """Detect explicit near-term entry-review requests without broad trade chatter."""
    normalized = " ".join(message.strip().split())
    if not normalized or NON_PREFLIGHT_CONTEXT.search(normalized):
        return False
    return any(pattern.search(normalized) for pattern in PREFLIGHT_INTENT_PATTERNS)


@dataclass(frozen=True)
class PretradeAlert:
    event_id: str
    title: str
    scheduled_at: datetime
    country: str
    currency: str | None
    importance: int
    minutes_from_now: int
    source_url: str | None
    retrieved_at: datetime


PreflightRating = Literal["eligible", "conditional", "stand_aside", "blocked"]
RuleStatus = Literal["met", "not_met", "missing", "triggered"]


@dataclass(frozen=True)
class StrategyRule:
    rule_id: str
    kind: Literal["requirement", "exclusion"]
    text: str
    scope: str


@dataclass(frozen=True)
class StrategyRuleResult:
    rule_id: str
    kind: Literal["requirement", "exclusion"]
    text: str
    scope: str
    status: RuleStatus


@dataclass(frozen=True)
class NewsReadiness:
    status: Literal["fresh", "stale", "not_configured", "unavailable"]
    latest_retrieved_at: datetime | None
    detail: str
    relevant_currencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreflightAssessment:
    rating: PreflightRating
    strategy_name: str
    strategy_version: int
    strategy_hash: str
    setup_key: str | None
    component_scores: dict[str, int]
    hard_blockers: tuple[str, ...]
    stand_aside_reasons: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    rule_results: tuple[StrategyRuleResult, ...]
    news: NewsReadiness
    alerts: tuple[PretradeAlert, ...]
    disclaimer: str = (
        "This grades adherence to the selected strategy; it does not predict the "
        "outcome or place an order. The trader retains the final decision."
    )


@dataclass(frozen=True)
class PersistedPreflight:
    assessment: PretradeAssessment
    mindset: MindsetCheckInRead
    trade_plan: TradePlan | None


def record_preflight_assessment(
    db: Session,
    assessment: PreflightAssessment,
    *,
    playbook_version_id: uuid.UUID,
    mindset_checkin_id: uuid.UUID,
    policy_hash: str,
    market_context: dict | None = None,
    commit: bool = True,
) -> PretradeAssessment:
    mindset = db.get(MindsetCheckIn, mindset_checkin_id)
    if mindset is None:
        raise LookupError(f"mindset check-in was not found: {mindset_checkin_id}")
    if mindset.playbook_version_id != playbook_version_id:
        raise ValueError(
            "mindset check-in and assessment strategy versions do not match"
        )
    record = PretradeAssessment(
        playbook_version_id=playbook_version_id,
        mindset_checkin_id=mindset_checkin_id,
        setup_key=assessment.setup_key,
        rating=assessment.rating,
        component_scores=assessment.component_scores,
        hard_blockers=list(assessment.hard_blockers),
        stand_aside_reasons=list(assessment.stand_aside_reasons),
        missing_evidence=list(assessment.missing_evidence),
        rule_results=[
            {
                "rule_id": item.rule_id,
                "kind": item.kind,
                "text": item.text,
                "scope": item.scope,
                "status": item.status,
            }
            for item in assessment.rule_results
        ],
        news_status=assessment.news.status,
        market_context=market_context or {},
        policy_hash=policy_hash,
        human_decision="pending",
    )
    db.add(record)
    db.flush()
    if commit:
        db.commit()
        db.refresh(record)
    return record


def finalize_preflight_assessment(
    db: Session,
    assessment_id: uuid.UUID,
    *,
    decision: Literal["proceed", "stand_aside", "cancelled"],
    trade_plan: TradePlan | None = None,
    commit: bool = True,
) -> PretradeAssessment:
    record = db.get(PretradeAssessment, assessment_id)
    if record is None:
        raise LookupError(f"pre-trade assessment was not found: {assessment_id}")
    if record.human_decision != "pending":
        raise ValueError("pre-trade assessment already has a final human decision")
    if decision == "proceed" and trade_plan is None:
        raise ValueError("a proceed decision must link a journaled trade plan")
    if decision == "proceed" and record.rating not in {"eligible", "conditional"}:
        raise ValueError(
            f"a {record.rating} assessment cannot be finalized as proceed"
        )
    if decision != "proceed" and trade_plan is not None:
        raise ValueError("only a proceed decision can link a trade plan")
    if (
        trade_plan is not None
        and trade_plan.playbook_version_id != record.playbook_version_id
    ):
        raise ValueError("trade plan and assessment strategy versions do not match")
    record.human_decision = decision
    record.trade_plan_id = trade_plan.id if trade_plan is not None else None
    record.decided_at = datetime.now(UTC)
    if trade_plan is not None and record.mindset_checkin_id is not None:
        mindset = db.get(MindsetCheckIn, record.mindset_checkin_id)
        if mindset is not None:
            mindset.trade_plan_id = trade_plan.id
    db.flush()
    if commit:
        db.commit()
        db.refresh(record)
    return record


def persist_preflight_workflow(
    db: Session,
    *,
    assessment: PreflightAssessment,
    playbook_version_id: uuid.UUID,
    mindset_request: MindsetCheckInCreate,
    decision: Literal["proceed", "stand_aside", "cancelled"],
    policy_hash: str,
    trade_request: TradePlanCreate | None = None,
    maximum_risk_percent: Decimal = Decimal("1"),
    market_context: dict | None = None,
    trade_creator: Callable[..., TradePlan] = create_trade_plan,
) -> PersistedPreflight:
    """Persist the complete preflight audit in one commit or roll it all back."""
    if decision == "proceed" and trade_request is None:
        raise ValueError("a proceed decision requires a trade request")
    if decision != "proceed" and trade_request is not None:
        raise ValueError("only a proceed decision can include a trade request")
    if db.new or db.dirty or db.deleted:
        raise RuntimeError(
            "preflight persistence requires a clean session so unrelated pending "
            "changes cannot be committed or rolled back"
        )
    joined_read_transaction = db.in_transaction()
    transaction = db.begin_nested() if joined_read_transaction else db.begin()
    with transaction:
        version = db.get(PlaybookVersion, playbook_version_id)
        playbook = db.get(Playbook, version.playbook_id) if version else None
        if version is None or playbook is None:
            raise LookupError(f"strategy version was not found: {playbook_version_id}")
        if (
            assessment.strategy_name != playbook.name
            or assessment.strategy_version != version.version
            or assessment.strategy_hash != version.content_hash
        ):
            raise ValueError(
                "assessment identity does not match the exact immutable strategy version"
            )
        mindset = create_mindset_check_in(
            db,
            mindset_request,
            playbook_version_id=playbook_version_id,
            commit=False,
        )
        record = record_preflight_assessment(
            db,
            assessment,
            playbook_version_id=playbook_version_id,
            mindset_checkin_id=mindset.id,
            policy_hash=policy_hash,
            market_context=market_context,
            commit=False,
        )
        trade = None
        if trade_request is not None:
            trade = trade_creator(
                db,
                trade_request,
                policy_hash=policy_hash,
                source="preflight",
                maximum_risk_percent=maximum_risk_percent,
                playbook_version_id=playbook_version_id,
                commit=False,
            )
        finalized = finalize_preflight_assessment(
            db,
            record.id,
            decision=decision,
            trade_plan=trade,
            commit=False,
        )
    if joined_read_transaction:
        db.commit()
    return PersistedPreflight(
        assessment=finalized,
        mindset=mindset,
        trade_plan=trade,
    )


TOP_LEVEL_STRATEGY_FIELDS = frozenset(
    {
        "methodology",
        "objective",
        "composition",
        "requirements",
        "exclusions",
        "context",
        "setups",
        "allowed_vocabulary",
        "forbidden_cross_strategy_concepts",
        "mindset",
        "risk",
    }
)
CONTEXT_FIELDS = frozenset({"required", "exclusions"})
SETUP_FIELDS = frozenset({"key", "requirements", "exclusions"})
COMPOSITION_FIELDS = frozenset({"wyckoff_role", "ict_role", "conflict_rule"})
RISK_FIELDS = frozenset(
    {"maximum_risk_percent", "minimum_planned_r", "human_confirms_every_trade"}
)
MINDSET_FIELDS = frozenset({"caution_emotion_tags"})


def _reject_unknown_fields(value: dict, allowed: frozenset[str], scope: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"unsupported {scope} field(s): {', '.join(unknown)}; "
            "preflight fails closed until they have explicit rule semantics"
        )


def _strings(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of non-empty strings")
    strings = tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )
    if len(strings) != len(value):
        raise ValueError(f"{field} must contain only non-empty strings")
    return strings


def _rule_id(
    *,
    kind: Literal["requirement", "exclusion"],
    scope: str,
    index: int,
    text: str,
) -> str:
    digest = hashlib.sha256(
        f"{kind}\0{scope}\0{index}\0{text}".encode()
    ).hexdigest()[:12]
    return f"{scope}:{kind}:{index}:{digest}"


def strategy_rules(
    definition: dict,
    *,
    setup_key: str | None = None,
) -> tuple[StrategyRule, ...]:
    """Return the exact user-authored rules for one explicitly selected setup."""
    if not isinstance(definition, dict):
        raise ValueError("strategy definition must be an object")
    _reject_unknown_fields(
        definition,
        TOP_LEVEL_STRATEGY_FIELDS,
        "top-level strategy",
    )
    rules: list[StrategyRule] = []

    def add(kind: Literal["requirement", "exclusion"], values: object, scope: str) -> None:
        texts = _strings(values, field=f"{scope}.{kind}s")
        rules.extend(
            StrategyRule(
                _rule_id(kind=kind, scope=scope, index=index, text=text),
                kind,
                text,
                scope,
            )
            for index, text in enumerate(texts)
        )

    add("requirement", definition.get("requirements"), "strategy")
    add("exclusion", definition.get("exclusions"), "strategy")
    context = definition.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise ValueError("context must be an object")
        _reject_unknown_fields(context, CONTEXT_FIELDS, "context")
        add("requirement", context.get("required"), "context")
        add("exclusion", context.get("exclusions"), "context")

    setups = definition.get("setups")
    if setups is not None and not isinstance(setups, list):
        raise ValueError("setups must be a list")
    if isinstance(setups, list) and setups:
        for index, item in enumerate(setups):
            if not isinstance(item, dict):
                raise ValueError(f"setups[{index}] must be an object")
            _reject_unknown_fields(item, SETUP_FIELDS, f"setups[{index}]")
        available = [
            item for item in setups
            if isinstance(item.get("key"), str) and item["key"].strip()
        ]
        if len(available) != len(setups):
            raise ValueError("every setup must have a non-empty string key")
        keys = [str(item["key"]).strip().lower() for item in available]
        if len(keys) != len(set(keys)):
            raise ValueError("setup keys must be unique")
        if setup_key is None:
            if len(available) > 1:
                names = ", ".join(str(item["key"]) for item in available)
                raise ValueError(f"select one setup key: {names}")
            selected = available[0] if available else None
        else:
            selected = next(
                (
                    item
                    for item in available
                    if str(item["key"]).strip().lower() == setup_key.strip().lower()
                ),
                None,
            )
            if selected is None:
                raise ValueError(f"setup key is not defined by this strategy: {setup_key}")
        if selected is not None:
            scope = f"setup:{selected['key']}"
            add("requirement", selected.get("requirements"), scope)
            add("exclusion", selected.get("exclusions"), scope)
    composition = definition.get("composition")
    if composition is not None:
        if not isinstance(composition, dict):
            raise ValueError("composition must be an object")
        _reject_unknown_fields(composition, COMPOSITION_FIELDS, "composition")
        conflict_rule = composition.get("conflict_rule")
        if conflict_rule is not None:
            if not isinstance(conflict_rule, str) or not conflict_rule.strip():
                raise ValueError("composition.conflict_rule must be a non-empty string")
            add("exclusion", [conflict_rule], "composition")
    forbidden = _strings(
        definition.get("forbidden_cross_strategy_concepts"),
        field="forbidden_cross_strategy_concepts",
    )
    add(
        "exclusion",
        [
            f"Analysis uses forbidden cross-strategy concept: {concept}"
            for concept in forbidden
        ],
        "strategy_isolation",
    )
    _strings(
        definition.get("allowed_vocabulary"),
        field="allowed_vocabulary",
    )
    risk = definition.get("risk")
    if risk is not None:
        if not isinstance(risk, dict):
            raise ValueError("risk must be an object")
        _reject_unknown_fields(risk, RISK_FIELDS, "risk")
    mindset = definition.get("mindset")
    if mindset is not None:
        if not isinstance(mindset, dict):
            raise ValueError("mindset must be an object")
        _reject_unknown_fields(mindset, MINDSET_FIELDS, "mindset")
        _strings(
            mindset.get("caution_emotion_tags"),
            field="mindset.caution_emotion_tags",
        )
    if not rules:
        raise ValueError(
            "strategy has no enforceable preflight rules; add at least one "
            "requirement, exclusion, conflict rule, or forbidden concept"
        )
    return tuple(rules)


def news_readiness(
    db: Session,
    *,
    currencies: frozenset[str],
    now: datetime | None = None,
    configured: bool,
    stale_after: timedelta = timedelta(hours=24),
) -> NewsReadiness:
    if not configured:
        return NewsReadiness(
            status="not_configured",
            latest_retrieved_at=None,
            detail="No news/calendar connector is configured.",
            relevant_currencies=tuple(sorted(currencies)),
        )
    if not currencies:
        return NewsReadiness(
            status="unavailable",
            latest_retrieved_at=None,
            detail=(
                "No relevant economic-event currencies could be derived for "
                "this instrument."
            ),
        )
    current = now or datetime.now(UTC)
    latest = db.scalar(
        select(EconomicEvent.retrieved_at)
        .where(
            func.upper(EconomicEvent.currency).in_(currencies),
            EconomicEvent.scheduled_at >= current - stale_after,
            EconomicEvent.scheduled_at <= current + stale_after,
        )
        .order_by(EconomicEvent.retrieved_at.desc())
        .limit(1)
    )
    if latest is None:
        return NewsReadiness(
            status="unavailable",
            latest_retrieved_at=None,
            detail=(
                "The news connector is configured but no calendar evidence is "
                f"stored for {', '.join(sorted(currencies))}."
            ),
            relevant_currencies=tuple(sorted(currencies)),
        )
    if current - latest > stale_after:
        return NewsReadiness(
            status="stale",
            latest_retrieved_at=latest,
            detail=f"Latest calendar retrieval is older than {stale_after}.",
            relevant_currencies=tuple(sorted(currencies)),
        )
    return NewsReadiness(
        status="fresh",
        latest_retrieved_at=latest,
        detail=(
            "Stored calendar evidence for "
            f"{', '.join(sorted(currencies))} is within the freshness window."
        ),
        relevant_currencies=tuple(sorted(currencies)),
    )


def assess_preflight(
    *,
    strategy_name: str,
    strategy_version: int,
    strategy_hash: str,
    definition: dict,
    setup_key: str | None,
    rule_answers: dict[str, bool | None],
    risk_percent: Decimal,
    planned_r: Decimal | None,
    configured_maximum_risk_percent: Decimal,
    readiness: int,
    accepted_risk: bool,
    emotion_tags: list[str] | tuple[str, ...] = (),
    has_thesis: bool,
    has_invalidation: bool,
    observation_count: int,
    hypothesis_count: int,
    news: NewsReadiness,
    alerts: list[PretradeAlert],
) -> PreflightAssessment:
    rules = strategy_rules(definition, setup_key=setup_key)
    rule_results: list[StrategyRuleResult] = []
    missing: list[str] = []
    stand_aside: list[str] = []
    blockers: list[str] = []

    risk_definition = definition.get("risk")
    risk_definition = risk_definition if isinstance(risk_definition, dict) else {}
    strategy_max = Decimal(str(
        risk_definition.get("maximum_risk_percent", configured_maximum_risk_percent)
    ))
    maximum_risk = min(configured_maximum_risk_percent, strategy_max)
    minimum_r = (
        Decimal(str(risk_definition["minimum_planned_r"]))
        if risk_definition.get("minimum_planned_r") is not None
        else None
    )
    if risk_percent > maximum_risk:
        blockers.append(
            f"Risk {risk_percent}% exceeds the effective {maximum_risk}% maximum."
        )
    if minimum_r is not None and (planned_r is None or planned_r < minimum_r):
        blockers.append(
            f"Planned R {planned_r if planned_r is not None else 'missing'} is below "
            f"the strategy minimum {minimum_r}."
        )
    if not accepted_risk:
        blockers.append("Predefined risk has not been accepted.")
    if not 1 <= readiness <= 5:
        raise ValueError("readiness must be between 1 and 5")
    if readiness <= 2:
        stand_aside.append(
            f"Readiness is {readiness}/5; the deterministic mindset policy "
            "requires standing aside at readiness 1-2."
        )
    elif readiness == 3:
        missing.append(
            "Readiness is 3/5; pause and resolve the stated caution before proceeding."
        )
    mindset_policy = definition.get("mindset")
    mindset_policy = mindset_policy if isinstance(mindset_policy, dict) else {}
    caution_tags = {
        tag.lower()
        for tag in _strings(
            mindset_policy.get("caution_emotion_tags"),
            field="mindset.caution_emotion_tags",
        )
    }
    present_cautions = sorted(
        caution_tags
        & {tag.strip().lower() for tag in emotion_tags if tag.strip()}
    )
    missing.extend(
        f"Strategy-configured caution emotion tag is present: {tag}."
        for tag in present_cautions
    )

    for rule in rules:
        answer = rule_answers.get(rule.rule_id)
        if rule.kind == "requirement":
            status: RuleStatus = (
                "met" if answer is True else "not_met" if answer is False else "missing"
            )
            if status == "not_met":
                stand_aside.append(f"Required rule is not met: {rule.text}")
            elif status == "missing":
                missing.append(f"Unconfirmed requirement: {rule.text}")
        else:
            status = (
                "triggered" if answer is True else "met" if answer is False else "missing"
            )
            if status == "triggered":
                stand_aside.append(f"Exclusion applies: {rule.text}")
            elif status == "missing":
                missing.append(f"Unconfirmed exclusion: {rule.text}")
        rule_results.append(
            StrategyRuleResult(
                rule.rule_id,
                rule.kind,
                rule.text,
                rule.scope,
                status,
            )
        )

    evidence_checks = {
        "Thesis is missing.": has_thesis,
        "Invalidation is missing.": has_invalidation,
        "At least one direct observation is required.": observation_count > 0,
        "At least one explicitly labeled hypothesis is required.": hypothesis_count > 0,
    }
    missing.extend(label for label, present in evidence_checks.items() if not present)

    if news.status != "fresh":
        missing.append(f"News/calendar evidence is {news.status}: {news.detail}")
    news_exclusion = any(
        rule.kind == "exclusion"
        and "high-impact" in rule.text.lower()
        and "event" in rule.text.lower()
        for rule in rules
    )
    if any(alert.importance >= 3 for alert in alerts) and news_exclusion:
        stand_aside.append(
            "A stored high-impact event falls inside the configured pre-trade window."
        )

    requirement_results = [
        item for item in rule_results if item.kind == "requirement"
    ]
    met_requirements = sum(item.status == "met" for item in requirement_results)
    strategy_score = (
        round(100 * met_requirements / len(requirement_results))
        if requirement_results
        else 100
    )
    evidence_score = round(100 * sum(evidence_checks.values()) / len(evidence_checks))
    risk_score = 0 if any(
        item.startswith(("Risk ", "Planned R ")) for item in blockers
    ) else 100
    mindset_score = 0 if not accepted_risk else round(readiness / 5 * 100)
    news_score = 100 if news.status == "fresh" and not alerts else (
        50 if news.status == "fresh" else 0
    )

    rating: PreflightRating
    if blockers:
        rating = "blocked"
    elif stand_aside:
        rating = "stand_aside"
    elif missing:
        rating = "conditional"
    else:
        rating = "eligible"
    return PreflightAssessment(
        rating=rating,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        strategy_hash=strategy_hash,
        setup_key=setup_key,
        component_scores={
            "strategy": strategy_score,
            "risk": risk_score,
            "mindset": mindset_score,
            "evidence": evidence_score,
            "news": news_score,
        },
        hard_blockers=tuple(dict.fromkeys(blockers)),
        stand_aside_reasons=tuple(dict.fromkeys(stand_aside)),
        missing_evidence=tuple(dict.fromkeys(missing)),
        rule_results=tuple(rule_results),
        news=news,
        alerts=tuple(alerts),
    )


async def refresh_startup_calendar(
    settings: Settings,
    db: Session,
    *,
    today: date | None = None,
) -> int:
    if not settings.trading_economics_api_key:
        return 0
    start = today or datetime.now(UTC).date()
    connector = create_news_connector(settings)
    try:
        events = await connector.calendar(
            start=start,
            end=start + timedelta(days=settings.startup_news_horizon_days),
            countries=[],
            minimum_importance=settings.pretrade_minimum_event_importance,
        )
    finally:
        await connector.aclose()
    return store_calendar_events(db, events)


def pretrade_alerts(
    db: Session,
    message: str,
    *,
    currencies: frozenset[str],
    now: datetime | None = None,
    window_minutes: int = 120,
    minimum_importance: int = 2,
) -> list[PretradeAlert]:
    if not TRADE_INTENT.search(message):
        return []
    if not currencies:
        return []
    current = now or datetime.now(UTC)
    start = current - timedelta(minutes=15)
    end = current + timedelta(minutes=window_minutes)
    events = list(
        db.scalars(
            select(EconomicEvent)
            .where(
                EconomicEvent.scheduled_at >= start,
                EconomicEvent.scheduled_at <= end,
                EconomicEvent.importance >= minimum_importance,
                func.upper(EconomicEvent.currency).in_(currencies),
            )
            .order_by(
                EconomicEvent.scheduled_at,
                EconomicEvent.importance.desc(),
            )
            .limit(20)
        )
    )
    return [
        PretradeAlert(
            event_id=str(event.id),
            title=event.title,
            scheduled_at=event.scheduled_at,
            country=event.country,
            currency=event.currency,
            importance=event.importance,
            minutes_from_now=round(
                (event.scheduled_at - current).total_seconds() / 60
            ),
            source_url=event.source_url,
            retrieved_at=event.retrieved_at,
        )
        for event in events
    ]


def render_pretrade_context(alerts: list[PretradeAlert]) -> str:
    if not alerts:
        return ""
    lines = [
        "PRE-TRADE ECONOMIC EVENT ALERTS (timestamped evidence, not directional predictions)"
    ]
    for alert in alerts:
        lines.append(
            f"- {alert.scheduled_at.isoformat()} | importance={alert.importance} | "
            f"{alert.country}/{alert.currency or 'n/a'} | {alert.title} | "
            f"minutes_from_now={alert.minutes_from_now} | "
            f"source={alert.source_url or 'stored provider metadata'}"
        )
    return "\n".join(lines)
