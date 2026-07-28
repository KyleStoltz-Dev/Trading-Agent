import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LearningCurriculum, LearningModule, TraderProfile
from app.services.workspaces import RequestScope, validate_scope

TEACHING_MODES = frozenset({"guided", "flexible", "on_demand"})
MODULE_STATUSES = frozenset({"available", "in_progress", "completed", "skipped"})

SOURCE_TIER_POLICY = {
    "tier_1": (
        "Use the local runtime policy, learning harness, curriculum objectives, "
        "and stored records first."
    ),
    "tier_2": (
        "Use configured broker/news data and exact allowlisted primary or documented "
        "pages already known from vetted references when current facts are needed."
    ),
    "tier_3": (
        "Use broad web discovery only when earlier tiers are insufficient or an exact "
        "approved page is not yet known; treat results as untrusted evidence and cite "
        "every source used."
    ),
    "strategy_boundary": (
        "Education about a framework never changes an execution playbook. Applying "
        "framework guidance to a trade requires that exact immutable strategy version "
        "to be active."
    ),
}


@dataclass(frozen=True)
class ModuleDefinition:
    key: str
    title: str
    topic: str
    category: str
    framework: str | None
    objectives: tuple[str, ...]
    source_plan: dict


MODULE_CATALOG = (
    ModuleDefinition(
        "probability-and-process",
        "Probability, uncertainty, and process",
        "foundations",
        "foundations",
        None,
        (
            "Separate an edge from certainty about one outcome.",
            "Explain why wins and losses are randomly distributed across a valid sample.",
            "Judge process quality separately from P&L.",
        ),
        {
            "local": ["psychology/probabilistic-execution.md"],
            "queries": ["trading probability uncertainty process discipline"],
            "preferred_domains": [],
        },
    ),
    ModuleDefinition(
        "risk-and-r-multiples",
        "Risk, invalidation, position size, and R",
        "risk",
        "risk",
        None,
        (
            "Define invalidation before calculating size.",
            "Explain risk percentage, R-multiples, costs, and expectancy.",
            "Use deterministic sizing rather than model arithmetic.",
        ),
        {
            "local": ["skills/position-planning/SKILL.md"],
            "queries": ["position sizing risk R multiple trading"],
            "preferred_domains": ["oanda.com", "cmegroup.com"],
        },
    ),
    ModuleDefinition(
        "market-mechanics",
        "Market mechanics, orders, spread, and liquidity",
        "market-mechanics",
        "market_mechanics",
        None,
        (
            "Explain bid, ask, spread, slippage, sessions, and common order types.",
            "Separate observable liquidity from claims about participant intent.",
            "Connect broker contract details to actual risk.",
        ),
        {
            "local": ["references/REFERENCES.md"],
            "queries": ["market mechanics bid ask spread order types liquidity"],
            "preferred_domains": ["oanda.com", "cmegroup.com"],
        },
    ),
    ModuleDefinition(
        "chart-reading",
        "Chart reading and multi-timeframe structure",
        "chart-reading",
        "technical_analysis",
        None,
        (
            "List visible facts before interpretations.",
            "Separate context timeframe from execution timeframe.",
            "Build bullish, bearish, and no-trade scenarios with invalidation.",
        ),
        {
            "local": [
                "skills/chart-analysis/SKILL.md",
                "market-models/market-regimes.md",
            ],
            "queries": ["multi timeframe chart structure market regimes"],
            "preferred_domains": ["tradingview.com", "cmegroup.com"],
        },
    ),
    ModuleDefinition(
        "news-and-macro",
        "Economic news, macro releases, and event risk",
        "news-macro",
        "news",
        None,
        (
            "Read scheduled time, importance, actual, forecast, and previous values.",
            "Explain how event risk can change volatility without promising direction.",
            "Distinguish sourced news evidence from a manipulation narrative.",
        ),
        {
            "local": ["skills/premarket-planning/SKILL.md"],
            "queries": ["economic calendar actual forecast previous event volatility"],
            "preferred_domains": [
                "federalreserve.gov",
                "bls.gov",
                "bea.gov",
                "tradingeconomics.com",
            ],
        },
    ),
    ModuleDefinition(
        "retail-technical-strategies",
        "Common retail technical strategies",
        "retail-strategies",
        "strategy_survey",
        "retail-technical",
        (
            "Explain trend, breakout, mean-reversion, support/resistance, "
            "and indicator approaches.",
            "Turn any strategy label into observable entry, invalidation, and exclusion rules.",
            "Identify evidence and testing requirements instead of declaring a universal edge.",
        ),
        {
            "local": ["skills/edge-analysis/SKILL.md"],
            "queries": ["trend breakout mean reversion technical analysis strategies"],
            "preferred_domains": ["cmegroup.com", "tradingview.com"],
        },
    ),
    ModuleDefinition(
        "wyckoff-framework",
        "Wyckoff as a testable framework",
        "wyckoff",
        "strategy_framework",
        "wyckoff",
        (
            "Describe ranges, phases, springs, upthrusts, tests, and follow-through.",
            "Treat labels as provisional hypotheses tied to observable behavior.",
            "Keep Wyckoff education isolated from ICT/SMC execution rules.",
        ),
        {
            "local": ["market-models/wyckoff.md"],
            "queries": ["Wyckoff accumulation distribution spring upthrust education"],
            "preferred_domains": [],
        },
    ),
    ModuleDefinition(
        "ict-smc-framework",
        "ICT/SMC concepts as testable hypotheses",
        "ict-smc",
        "strategy_framework",
        "ict-smc",
        (
            "Operationalize liquidity, displacement, imbalance, and mitigation.",
            "Separate a level of interest from an entry confirmation.",
            "Keep ICT/SMC education isolated from pure Wyckoff execution rules.",
        ),
        {
            "local": ["market-models/liquidity-imbalance.md"],
            "queries": ["liquidity sweep displacement imbalance mitigation trading"],
            "preferred_domains": [],
        },
    ),
    ModuleDefinition(
        "journal-backtest-forward-test",
        "Journaling, backtesting, and forward testing",
        "testing",
        "testing",
        None,
        (
            "Freeze rules before collecting a sample.",
            "Record eligible, excluded, and unclear examples.",
            "Measure expectancy by setup, regime, session, timeframe, and news proximity.",
        ),
        {
            "local": [
                "skills/trade-review/SKILL.md",
                "skills/edge-analysis/SKILL.md",
            ],
            "queries": ["trading journal backtest forward test expectancy sample"],
            "preferred_domains": [],
        },
    ),
)

TOPIC_LABELS = {
    "foundations": "Probability and process",
    "risk": "Risk and position sizing",
    "market-mechanics": "Market mechanics",
    "chart-reading": "Chart reading",
    "news-macro": "News and macro events",
    "retail-strategies": "Retail technical strategies",
    "wyckoff": "Wyckoff",
    "ict-smc": "ICT/SMC",
    "testing": "Journaling and testing",
}


def is_learning_request(message: str) -> bool:
    normalized = " ".join(message.casefold().split())
    if re.search(
        r"\b(?:teach|learn|lesson|curriculum|education|course|"
        r"teaching mode|guided mode|flexible mode|on[ -]demand mode)\b",
        normalized,
    ):
        return True
    concepts = (
        "wyckoff",
        "spring",
        "upthrust",
        "ict",
        "smc",
        "smart money",
        "fvg",
        "fair value gap",
        "order block",
        "technical analysis",
        "indicator",
        "rsi",
        "macd",
        "moving average",
        "fibonacci",
        "support and resistance",
        "breakout",
        "market mechanics",
        "economic news",
        "cpi",
        "nfp",
        "interest rate",
        "treasury yield",
        "risk management",
        "backtest",
        "forward test",
    )
    return (
        any(concept in normalized for concept in concepts)
        and re.search(
            r"\b(?:what (?:is|are|does)|explain|understand|help me understand|"
            r"how (?:does|do)|why (?:does|do|is|are|can)|walk me through|"
            r"tell me about)\b",
            normalized,
        )
        is not None
    )


def default_teaching_mode(experience_level: str) -> str:
    return {
        "beginner": "guided",
        "intermediate": "flexible",
        "advanced": "on_demand",
    }.get(experience_level, "flexible")


def all_learning_topics() -> tuple[str, ...]:
    return tuple(TOPIC_LABELS)


def learning_source_paths_for_message(message: str) -> tuple[str, ...]:
    normalized = " ".join(message.casefold().split())
    paths: list[str] = []
    for definition in MODULE_CATALOG:
        if (
            f"lesson-{definition.key}" not in normalized
            and definition.key not in normalized
        ):
            continue
        for path in definition.source_plan.get("local", []):
            if isinstance(path, str) and path not in paths:
                paths.append(path)
    return tuple(paths)


def curriculum_for_profile(
    db: Session,
    profile_id,
    *,
    scope: RequestScope,
) -> LearningCurriculum | None:
    validate_scope(db, scope)
    return db.scalar(
        select(LearningCurriculum)
        .join(TraderProfile, TraderProfile.id == LearningCurriculum.profile_id)
        .where(
            LearningCurriculum.profile_id == profile_id,
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.account_id == scope.account_id,
        )
    )


def curriculum_modules(
    db: Session,
    curriculum_id,
    *,
    scope: RequestScope,
    included_only: bool = True,
) -> list[LearningModule]:
    validate_scope(db, scope)
    scoped_curriculum_id = db.scalar(
        select(LearningCurriculum.id)
        .join(TraderProfile, TraderProfile.id == LearningCurriculum.profile_id)
        .where(
            LearningCurriculum.id == curriculum_id,
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.account_id == scope.account_id,
        )
    )
    if scoped_curriculum_id is None:
        raise LookupError("learning curriculum was not found in the requested scope")
    statement = (
        select(LearningModule)
        .join(
            LearningCurriculum,
            LearningCurriculum.id == LearningModule.curriculum_id,
        )
        .join(TraderProfile, TraderProfile.id == LearningCurriculum.profile_id)
        .where(
            LearningModule.curriculum_id == curriculum_id,
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.account_id == scope.account_id,
        )
    )
    if included_only:
        statement = statement.where(LearningModule.included.is_(True))
    return list(
        db.scalars(
            statement.order_by(LearningModule.sequence, LearningModule.title)
        )
    )


def configure_learning_curriculum(
    db: Session,
    profile: TraderProfile,
    *,
    scope: RequestScope,
    experience_level: str,
    teaching_mode: str | None,
    selected_topics: list[str],
    commit: bool = True,
) -> LearningCurriculum | None:
    profile = _require_profile_scope(db, profile, scope=scope)
    existing = curriculum_for_profile(db, profile.id, scope=scope)
    if teaching_mode is None:
        if existing is not None:
            existing.status = "paused"
            if commit:
                db.commit()
                db.refresh(existing)
            else:
                db.flush()
        return existing
    if teaching_mode not in TEACHING_MODES:
        raise ValueError("teaching mode must be guided, flexible, or on_demand")
    topics = list(dict.fromkeys(selected_topics))
    unknown = sorted(set(topics) - set(TOPIC_LABELS))
    if unknown:
        raise ValueError(f"unknown learning topics: {', '.join(unknown)}")
    if not topics:
        raise ValueError("an enabled curriculum requires at least one topic")

    curriculum = existing
    if curriculum is None:
        curriculum = LearningCurriculum(
            profile_id=profile.id,
            experience_level=experience_level,
            teaching_mode=teaching_mode,
            status="active",
            selected_topics=topics,
            source_tier_policy=SOURCE_TIER_POLICY,
        )
        db.add(curriculum)
        db.flush()
    curriculum.experience_level = experience_level
    curriculum.teaching_mode = teaching_mode
    curriculum.status = "active"
    curriculum.selected_topics = topics
    curriculum.source_tier_policy = SOURCE_TIER_POLICY

    existing_modules = {
        module.module_key: module
        for module in curriculum_modules(
            db,
            curriculum.id,
            scope=scope,
            included_only=False,
        )
    }
    selected_definitions = [
        definition for definition in MODULE_CATALOG if definition.topic in topics
    ]
    selected_keys = {definition.key for definition in selected_definitions}
    for sequence, definition in enumerate(selected_definitions, start=1):
        module = existing_modules.get(definition.key)
        if module is None:
            module = LearningModule(
                curriculum_id=curriculum.id,
                module_key=definition.key,
            )
            db.add(module)
        module.title = definition.title
        module.category = definition.category
        module.framework = definition.framework
        module.sequence = sequence
        module.included = True
        if module.status == "skipped":
            module.status = "available"
        module.objectives = list(definition.objectives)
        module.source_plan = definition.source_plan
    for key, module in existing_modules.items():
        if not key.startswith("custom-") and key not in selected_keys:
            module.included = False
    custom_modules = sorted(
        (
            module
            for key, module in existing_modules.items()
            if key.startswith("custom-")
        ),
        key=lambda module: (module.sequence, module.title),
    )
    for sequence, module in enumerate(
        custom_modules,
        start=len(selected_definitions) + 1,
    ):
        module.sequence = sequence
        module.included = True
    if commit:
        db.commit()
        db.refresh(curriculum)
    else:
        db.flush()
    return curriculum


def curriculum_read(
    db: Session,
    curriculum: LearningCurriculum,
    *,
    scope: RequestScope,
) -> dict:
    curriculum = _require_curriculum_scope(db, curriculum, scope=scope)
    modules = curriculum_modules(db, curriculum.id, scope=scope)
    completed = sum(module.status == "completed" for module in modules)
    current = next(
        (
            module
            for module in modules
            if module.status == "in_progress"
        ),
        None,
    ) or next(
        (module for module in modules if module.status == "available"),
        None,
    )
    return {
        "teaching_mode": curriculum.teaching_mode,
        "experience_level": curriculum.experience_level,
        "status": curriculum.status,
        "selected_topics": curriculum.selected_topics,
        "progress": {
            "completed": completed,
            "total": len(modules),
            "percent": round(100 * completed / len(modules)) if modules else 0,
        },
        "next_module": _module_payload(current) if current else None,
        "modules": [_module_payload(module) for module in modules],
        "source_tier_policy": curriculum.source_tier_policy,
    }


def module_read(
    db: Session,
    module: LearningModule | None,
    *,
    scope: RequestScope,
) -> dict | None:
    if module is None:
        return None
    module = _require_module_scope(db, module, scope=scope)
    return _module_payload(module)


def _module_payload(module: LearningModule) -> dict:
    return {
        "reference": f"lesson-{module.module_key}",
        "key": module.module_key,
        "title": module.title,
        "category": module.category,
        "framework": module.framework,
        "sequence": module.sequence,
        "status": module.status,
        "objectives": module.objectives,
        "source_plan": module.source_plan,
        "evidence_references": module.evidence_references,
        "learner_notes": module.learner_notes,
        "started_at": module.started_at,
        "completed_at": module.completed_at,
    }


def add_custom_learning_module(
    db: Session,
    curriculum: LearningCurriculum,
    *,
    scope: RequestScope,
    title: str,
    category: str,
    framework: str | None,
    objectives: list[str],
    source_queries: list[str],
    preferred_domains: list[str],
) -> LearningModule:
    curriculum = _require_curriculum_scope(db, curriculum, scope=scope)
    clean_title = " ".join(title.split())
    if not 3 <= len(clean_title) <= 160:
        raise ValueError("lesson title must contain between 3 and 160 characters")
    clean_category = re.sub(r"[^a-z0-9-]+", "-", category.casefold()).strip("-")
    if not clean_category or len(clean_category) > 40:
        raise ValueError("lesson category must be a short name")
    if framework is not None and len(framework) > 80:
        raise ValueError("lesson framework cannot exceed 80 characters")
    clean_objectives = [" ".join(value.split()) for value in objectives if value.strip()]
    if not 1 <= len(clean_objectives) <= 8 or any(
        not 3 <= len(value) <= 500 for value in clean_objectives
    ):
        raise ValueError("provide between 1 and 8 concise lesson objectives")
    clean_queries = [" ".join(value.split()) for value in source_queries if value.strip()]
    if not 1 <= len(clean_queries) <= 5 or any(
        not 3 <= len(value) <= 200 for value in clean_queries
    ):
        raise ValueError("provide between 1 and 5 bounded source queries")
    clean_domains = list(
        dict.fromkeys(domain.strip().casefold() for domain in preferred_domains)
    )
    if len(clean_domains) > 10 or any(
        not re.fullmatch(r"[a-z0-9.-]{3,253}", domain) for domain in clean_domains
    ):
        raise ValueError("preferred domains must be valid domain names")

    slug = re.sub(r"[^a-z0-9]+", "-", clean_title.casefold()).strip("-")[:60]
    module_key = f"custom-{slug}"
    existing = db.scalar(
        select(LearningModule).where(
            LearningModule.curriculum_id == curriculum.id,
            LearningModule.module_key == module_key,
        )
    )
    if existing is not None:
        if existing.title != clean_title:
            raise ValueError("a different custom lesson already uses this reference")
        existing.included = True
        db.commit()
        db.refresh(existing)
        return existing

    modules = curriculum_modules(
        db,
        curriculum.id,
        scope=scope,
        included_only=False,
    )
    module = LearningModule(
        curriculum_id=curriculum.id,
        module_key=module_key,
        title=clean_title,
        category=clean_category,
        framework=framework.strip() if framework else None,
        sequence=max((item.sequence for item in modules), default=0) + 1,
        included=True,
        status="available",
        objectives=clean_objectives,
        source_plan={
            "local": [],
            "queries": clean_queries,
            "preferred_domains": clean_domains,
        },
    )
    db.add(module)
    db.commit()
    db.refresh(module)
    return module


def update_learning_module(
    db: Session,
    curriculum: LearningCurriculum,
    module_key: str,
    *,
    scope: RequestScope,
    status: str,
    learner_notes: str = "",
    evidence_references: list[dict] | None = None,
) -> LearningModule:
    curriculum = _require_curriculum_scope(db, curriculum, scope=scope)
    if status not in MODULE_STATUSES:
        raise ValueError(
            "lesson status must be available, in_progress, completed, or skipped"
        )
    module = db.scalar(
        select(LearningModule).where(
            LearningModule.curriculum_id == curriculum.id,
            LearningModule.module_key == module_key,
        )
    )
    if module is None:
        raise LookupError(f"lesson was not found in this curriculum: {module_key}")
    now = datetime.now(UTC)
    module.status = status
    if status == "in_progress" and module.started_at is None:
        module.started_at = now
    if status == "completed":
        module.started_at = module.started_at or now
        module.completed_at = now
    elif module.completed_at is not None:
        module.completed_at = None
    if learner_notes:
        if len(learner_notes) > 5_000:
            raise ValueError("learning notes cannot exceed 5000 characters")
        module.learner_notes = learner_notes
    if evidence_references is not None:
        module.evidence_references = evidence_references[:20]
    db.commit()
    db.refresh(module)
    return module


def _require_profile_scope(
    db: Session,
    profile: TraderProfile,
    *,
    scope: RequestScope,
) -> TraderProfile:
    """Reject a direct profile reference unless it belongs to the exact scope."""
    validate_scope(db, scope)
    scoped_profile = db.scalar(
        select(TraderProfile).where(
            TraderProfile.id == profile.id,
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.account_id == scope.account_id,
        )
    )
    if scoped_profile is None:
        raise LookupError("trader profile was not found in the requested scope")
    return scoped_profile


def _require_curriculum_scope(
    db: Session,
    curriculum: LearningCurriculum,
    *,
    scope: RequestScope,
) -> LearningCurriculum:
    """Reject a direct curriculum reference unless its profile is in scope."""
    validate_scope(db, scope)
    scoped_curriculum = db.scalar(
        select(LearningCurriculum)
        .join(TraderProfile, TraderProfile.id == LearningCurriculum.profile_id)
        .where(
            LearningCurriculum.id == curriculum.id,
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.account_id == scope.account_id,
        )
    )
    if scoped_curriculum is None:
        raise LookupError("learning curriculum was not found in the requested scope")
    return scoped_curriculum


def _require_module_scope(
    db: Session,
    module: LearningModule,
    *,
    scope: RequestScope,
) -> LearningModule:
    """Reject a direct module reference unless its curriculum profile is in scope."""
    validate_scope(db, scope)
    scoped_module = db.scalar(
        select(LearningModule)
        .join(
            LearningCurriculum,
            LearningCurriculum.id == LearningModule.curriculum_id,
        )
        .join(TraderProfile, TraderProfile.id == LearningCurriculum.profile_id)
        .where(
            LearningModule.id == module.id,
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.account_id == scope.account_id,
        )
    )
    if scoped_module is None:
        raise LookupError("learning module was not found in the requested scope")
    return scoped_module
