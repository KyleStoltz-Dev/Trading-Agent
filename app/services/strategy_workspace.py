import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import LEGACY_UNASSIGNED_ACCOUNT_ID, LEGACY_WORKSPACE_ID
from app.models import (
    ConversationSession,
    Playbook,
    PlaybookVersion,
    StrategyExperiment,
    StrategyKnowledgeItem,
    StrategyTestSample,
    TraderProfile,
)
from app.schemas import (
    KnowledgeItemRead,
    StrategyExperimentCreate,
    StrategyExperimentRead,
    StrategySummary,
    StrategyTestSampleCreate,
    TraderProfileRead,
    TraderProfileUpsert,
)
from app.services.catalog import verify_playbook_version_integrity
from app.services.workspaces import (
    RequestScope,
    validate_scope,
    validate_strategy_scope,
)

SEARCH_TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}")
KNOWLEDGE_REFERENCE = re.compile(r"^knowledge-([0-9a-f]{12})$")


def _legacy_scope(scope: RequestScope | None) -> RequestScope:
    return scope or RequestScope(
        workspace_id=uuid.UUID(LEGACY_WORKSPACE_ID),
        account_id=uuid.UUID(LEGACY_UNASSIGNED_ACCOUNT_ID),
    )


def get_trader_profile(
    db: Session,
    profile_key: str = "local",
    *,
    scope: RequestScope,
) -> TraderProfile | None:
    validate_scope(db, scope)
    return db.scalar(
        select(TraderProfile).where(
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.account_id == scope.account_id,
            TraderProfile.profile_key == profile_key,
        )
    )


def upsert_trader_profile(
    db: Session,
    request: TraderProfileUpsert,
    profile_key: str = "local",
    *,
    scope: RequestScope,
    commit: bool = True,
) -> TraderProfile:
    validate_scope(db, scope)
    profile = get_trader_profile(db, profile_key, scope=scope)
    if profile is None:
        profile = TraderProfile(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            profile_key=profile_key,
        )
        db.add(profile)
    for key, value in request.model_dump().items():
        setattr(profile, key, value)
    if commit:
        db.commit()
        db.refresh(profile)
    else:
        db.flush()
    return profile


def resolve_strategy_version(
    db: Session,
    strategy: str,
    version: int | None = None,
    *,
    scope: RequestScope | None = None,
) -> tuple[Playbook, PlaybookVersion]:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    normalized = strategy.strip().lower()
    playbook = db.scalar(
        select(Playbook).where(
            Playbook.workspace_id == scope.workspace_id,
            func.lower(Playbook.name) == normalized,
        )
    )
    if playbook is None:
        raise LookupError(f"strategy was not found: {strategy}")
    statement = select(PlaybookVersion).where(
        PlaybookVersion.workspace_id == scope.workspace_id,
        PlaybookVersion.playbook_id == playbook.id
    )
    if version is not None:
        statement = statement.where(PlaybookVersion.version == version)
    else:
        statement = statement.order_by(PlaybookVersion.version.desc()).limit(1)
    playbook_version = db.scalar(statement)
    if playbook_version is None:
        raise LookupError(f"strategy version was not found: {strategy}")
    verify_playbook_version_integrity(playbook_version)
    return playbook, playbook_version


def list_strategy_summaries(
    db: Session,
    *,
    scope: RequestScope | None = None,
) -> list[StrategySummary]:
    validate_scope(db, scope)
    playbooks = list(
        db.scalars(
            select(Playbook)
            .where(
                Playbook.workspace_id == scope.workspace_id,
                Playbook.active.is_(True),
            )
            .order_by(Playbook.name)
        )
    )
    summaries: list[StrategySummary] = []
    for playbook in playbooks:
        version = db.scalar(
            select(PlaybookVersion)
            .where(
                PlaybookVersion.workspace_id == scope.workspace_id,
                PlaybookVersion.playbook_id == playbook.id,
            )
            .order_by(PlaybookVersion.version.desc())
            .limit(1)
        )
        if version is None:
            continue
        count = db.scalar(
            select(func.count())
            .select_from(StrategyKnowledgeItem)
            .where(
                StrategyKnowledgeItem.playbook_version_id == version.id,
                StrategyKnowledgeItem.workspace_id == scope.workspace_id,
                StrategyKnowledgeItem.excluded.is_(False),
            )
        )
        summaries.append(
            StrategySummary(
                playbook_id=playbook.id,
                playbook_version_id=version.id,
                name=playbook.name,
                description=playbook.description,
                version=version.version,
                content_hash=version.content_hash,
                sample_requirement=version.sample_requirement,
                knowledge_items=int(count or 0),
            )
        )
    return summaries


def set_session_strategy(
    db: Session,
    conversation: ConversationSession,
    strategy: str | None,
    *,
    scope: RequestScope,
    version: int | None = None,
) -> tuple[Playbook, PlaybookVersion] | None:
    scope = _legacy_scope(scope)
    validate_scope(db, scope)
    if (
        conversation.workspace_id != scope.workspace_id
        or conversation.account_id != scope.account_id
    ):
        raise LookupError("conversation was not found in the requested account scope")
    if strategy is None:
        conversation.active_playbook_version_id = None
        db.commit()
        return None
    resolved = resolve_strategy_version(db, strategy, version, scope=scope)
    conversation.active_playbook_version_id = resolved[1].id
    db.commit()
    db.refresh(conversation)
    return resolved


def active_session_strategy(
    db: Session,
    conversation: ConversationSession,
    *,
    scope: RequestScope,
) -> tuple[Playbook, PlaybookVersion] | None:
    validate_scope(db, scope)
    if (
        conversation.workspace_id != scope.workspace_id
        or conversation.account_id != scope.account_id
    ):
        raise LookupError("conversation was not found in the requested account scope")
    if conversation.active_playbook_version_id is None:
        return None
    version = validate_strategy_scope(
        db,
        scope,
        conversation.active_playbook_version_id,
    )
    if version is None:
        return None
    verify_playbook_version_integrity(version)
    playbook = db.scalar(
        select(Playbook).where(
            Playbook.workspace_id == scope.workspace_id,
            Playbook.id == version.playbook_id,
        )
    )
    return (playbook, version) if playbook is not None else None


def strategy_by_version_id(
    db: Session,
    playbook_version_id: uuid.UUID | None,
    *,
    scope: RequestScope | None = None,
) -> tuple[Playbook, PlaybookVersion] | None:
    if playbook_version_id is None:
        return None
    if scope is None:
        version = db.get(PlaybookVersion, playbook_version_id)
        if version is None:
            return None
        playbook = db.get(Playbook, version.playbook_id)
        return (playbook, version) if playbook is not None else None
    validate_scope(db, scope)
    version = validate_strategy_scope(db, scope, playbook_version_id)
    if version is None:
        return None
    verify_playbook_version_integrity(version)
    playbook = db.scalar(
        select(Playbook).where(
            Playbook.workspace_id == scope.workspace_id,
            Playbook.id == version.playbook_id,
        )
    )
    return (playbook, version) if playbook is not None else None


def search_strategy_knowledge(
    db: Session,
    playbook_version_id: uuid.UUID,
    query: str,
    limit: int = 8,
    *,
    scope: RequestScope | None = None,
) -> list[StrategyKnowledgeItem]:
    if scope is not None:
        validate_strategy_scope(db, scope, playbook_version_id)
    if not 1 <= limit <= 25:
        raise ValueError("knowledge result limit must be between 1 and 25")
    terms = tuple(dict.fromkeys(term.lower() for term in SEARCH_TERM.findall(query)))
    if not terms:
        raise ValueError("knowledge query must include at least one searchable term")
    predicates = [
        func.lower(StrategyKnowledgeItem.content).contains(term)
        for term in terms[:8]
    ]
    statement = select(StrategyKnowledgeItem).where(
        StrategyKnowledgeItem.playbook_version_id == playbook_version_id,
        StrategyKnowledgeItem.excluded.is_(False),
        or_(*predicates),
    )
    if scope is not None:
        statement = statement.where(
            StrategyKnowledgeItem.workspace_id == scope.workspace_id
        )
    return list(
        db.scalars(
            statement
            .order_by(
                StrategyKnowledgeItem.occurred_at.desc().nullslast(),
                StrategyKnowledgeItem.created_at.desc(),
            )
            .limit(limit)
        )
    )


def knowledge_item_reference(item: StrategyKnowledgeItem) -> str:
    """Return a human-facing reference without exposing the row UUID."""
    return f"knowledge-{item.content_hash[:12]}"


def search_strategy_knowledge_for_management(
    db: Session,
    playbook_version_id: uuid.UUID,
    query: str,
    *,
    scope: RequestScope,
    status: str,
    limit: int = 8,
) -> list[StrategyKnowledgeItem]:
    """Find bounded candidates within one immutable strategy version."""
    validate_strategy_scope(db, scope, playbook_version_id)
    if status not in {"active", "quarantined"}:
        raise ValueError("knowledge status must be active or quarantined")
    if not 1 <= limit <= 20:
        raise ValueError("knowledge management result limit must be between 1 and 20")
    terms = tuple(dict.fromkeys(term.lower() for term in SEARCH_TERM.findall(query)))
    if not terms:
        raise ValueError("knowledge query must include at least one searchable term")
    predicates = [
        func.lower(StrategyKnowledgeItem.content).contains(term)
        for term in terms[:8]
    ]
    return list(
        db.scalars(
            select(StrategyKnowledgeItem)
            .where(
                StrategyKnowledgeItem.playbook_version_id == playbook_version_id,
                StrategyKnowledgeItem.workspace_id == scope.workspace_id,
                StrategyKnowledgeItem.excluded.is_(status == "quarantined"),
                or_(*predicates),
            )
            .order_by(
                StrategyKnowledgeItem.occurred_at.desc().nullslast(),
                StrategyKnowledgeItem.created_at.desc(),
            )
            .limit(limit)
        )
    )


def resolve_strategy_knowledge_reference(
    db: Session,
    playbook_version_id: uuid.UUID,
    reference: str,
    *,
    scope: RequestScope,
) -> StrategyKnowledgeItem:
    """Resolve one human reference, failing closed on malformed or ambiguous values."""
    validate_strategy_scope(db, scope, playbook_version_id)
    match = KNOWLEDGE_REFERENCE.fullmatch(reference.strip().lower())
    if match is None:
        raise ValueError(
            "knowledge reference must match knowledge- followed by 12 hexadecimal characters"
        )
    prefix = match.group(1)
    matches = list(
        db.scalars(
            select(StrategyKnowledgeItem)
            .where(
                StrategyKnowledgeItem.playbook_version_id == playbook_version_id,
                StrategyKnowledgeItem.workspace_id == scope.workspace_id,
                StrategyKnowledgeItem.content_hash.startswith(prefix),
            )
            .limit(2)
        )
    )
    if not matches:
        raise LookupError(
            "knowledge reference was not found in the active strategy version"
        )
    if len(matches) > 1:
        raise LookupError(
            "knowledge reference is ambiguous; search again for an exact candidate"
        )
    return matches[0]


def set_active_strategy_knowledge_excluded(
    db: Session,
    playbook_version_id: uuid.UUID,
    reference: str,
    *,
    scope: RequestScope,
    excluded: bool,
) -> StrategyKnowledgeItem:
    """Reversibly change one item already scoped by the host's active strategy."""
    item = resolve_strategy_knowledge_reference(
        db,
        playbook_version_id,
        reference,
        scope=scope,
    )
    if item.excluded is excluded:
        state = "quarantined" if excluded else "active"
        raise ValueError(f"knowledge item is already {state}")
    item.excluded = excluded
    db.commit()
    db.refresh(item)
    return item


def set_strategy_knowledge_excluded(
    db: Session,
    strategy: str,
    item_id: uuid.UUID,
    *,
    scope: RequestScope,
    excluded: bool,
) -> StrategyKnowledgeItem:
    _, version = resolve_strategy_version(db, strategy, scope=scope)
    item = db.scalar(
        select(StrategyKnowledgeItem).where(
            StrategyKnowledgeItem.workspace_id == scope.workspace_id,
            StrategyKnowledgeItem.id == item_id,
        )
    )
    if item is None or item.playbook_version_id != version.id:
        raise LookupError(
            f"knowledge item was not found in {strategy}: {item_id}"
        )
    item.excluded = excluded
    db.commit()
    db.refresh(item)
    return item


def create_strategy_experiment(
    db: Session,
    request: StrategyExperimentCreate,
    *,
    scope: RequestScope,
) -> StrategyExperiment:
    _, version = resolve_strategy_version(db, request.strategy, scope=scope)
    experiment = StrategyExperiment(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        playbook_version_id=version.id,
        name=request.name,
        mode=request.mode,
        status="running",
        hypothesis=request.hypothesis,
        instrument=request.instrument,
        timeframe=request.timeframe,
        data_start=request.data_start,
        data_end=request.data_end,
        rules_hash=version.content_hash,
    )
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    return experiment


def resolve_strategy_experiment(
    db: Session,
    reference: str | uuid.UUID,
    *,
    scope: RequestScope | None = None,
    playbook_version_id: uuid.UUID | None = None,
) -> StrategyExperiment:
    if scope is not None:
        validate_strategy_scope(db, scope, playbook_version_id)
    try:
        identifier = (
            reference if isinstance(reference, uuid.UUID) else uuid.UUID(reference)
        )
    except ValueError:
        statement = select(StrategyExperiment).where(
            func.lower(StrategyExperiment.name) == str(reference).strip().lower()
        )
        if scope is not None:
            statement = statement.where(
                StrategyExperiment.workspace_id == scope.workspace_id,
                StrategyExperiment.account_id == scope.account_id,
            )
        if playbook_version_id is not None:
            statement = statement.where(
                StrategyExperiment.playbook_version_id == playbook_version_id
            )
        matches = list(db.scalars(statement.limit(2)))
        if not matches:
            raise LookupError(
                f"strategy experiment was not found: {reference}"
            ) from None
        if len(matches) > 1:
            raise LookupError(
                f"experiment name is ambiguous: {reference}; use its internal UUID"
            ) from None
        return matches[0]
    if scope is None:
        experiment = db.get(StrategyExperiment, identifier)
    else:
        experiment = db.scalar(
            select(StrategyExperiment).where(
                StrategyExperiment.workspace_id == scope.workspace_id,
                StrategyExperiment.account_id == scope.account_id,
                StrategyExperiment.id == identifier,
            )
        )
    if experiment is None:
        raise LookupError(f"strategy experiment was not found: {reference}")
    if (
        playbook_version_id is not None
        and experiment.playbook_version_id != playbook_version_id
    ):
        raise PermissionError(
            "experiment belongs to a different strategy version than the active session"
        )
    return experiment


def add_strategy_test_sample(
    db: Session,
    experiment_id: str | uuid.UUID,
    request: StrategyTestSampleCreate,
    *,
    scope: RequestScope,
) -> StrategyTestSample:
    experiment = resolve_strategy_experiment(db, experiment_id, scope=scope)
    if experiment.status != "running":
        raise ValueError("samples can only be added to a running experiment")
    sample = StrategyTestSample(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        experiment_id=experiment.id,
        **request.model_dump(),
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def complete_strategy_experiment(
    db: Session,
    experiment_id: str | uuid.UUID,
    *,
    scope: RequestScope,
) -> StrategyExperiment:
    experiment = resolve_strategy_experiment(db, experiment_id, scope=scope)
    experiment.status = "completed"
    experiment.completed_at = datetime.now(UTC)
    db.commit()
    db.refresh(experiment)
    return experiment


def profile_read(profile: TraderProfile) -> TraderProfileRead:
    return TraderProfileRead.model_validate(profile)


def knowledge_reads(items: list[StrategyKnowledgeItem]) -> list[KnowledgeItemRead]:
    return [KnowledgeItemRead.model_validate(item) for item in items]


def experiment_read(experiment: StrategyExperiment) -> StrategyExperimentRead:
    return StrategyExperimentRead.model_validate(experiment)
