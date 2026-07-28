import hashlib
import json
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalysisRun, EvidenceItem, Observation, TradePlan
from app.providers import ModelProvider
from app.schemas import ChartAnalysis
from app.services.workspaces import RequestScope, validate_scope

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _scoped_evidence_directory(
    directory: Path,
    scope: RequestScope,
) -> Path:
    """Create each account boundary without following tenant-controlled symlinks."""
    current = directory.expanduser().absolute()
    for component in (str(scope.workspace_id), str(scope.account_id)):
        if current.is_symlink():
            raise ValueError("evidence directory cannot be a symlink")
        current.mkdir(parents=True, exist_ok=True, mode=0o700)
        current.chmod(0o700)
        current = current / component
    if current.is_symlink():
        raise ValueError("evidence directory cannot be a symlink")
    current.mkdir(exist_ok=True, mode=0o700)
    current.chmod(0o700)
    return current


def store_evidence_file(
    data: bytes,
    content_type: str,
    directory: Path,
) -> tuple[Path, str]:
    extension = EXTENSIONS.get(content_type)
    if extension is None:
        raise ValueError("unsupported evidence content type")
    digest = _sha256(data)
    root = directory.expanduser().absolute()
    if root.is_symlink():
        raise ValueError("evidence directory cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    target_directory = root / digest[:2]
    if target_directory.is_symlink():
        raise ValueError("evidence digest directory cannot be a symlink")
    target_directory.mkdir(exist_ok=True, mode=0o700)
    target_directory.chmod(0o700)
    target = target_directory / f"{digest}{extension}"
    if target.is_symlink():
        raise ValueError("evidence target cannot be a symlink")
    if target.exists():
        target_stat = target.stat()
        if not stat.S_ISREG(target_stat.st_mode) or _sha256(target.read_bytes()) != digest:
            raise ValueError("existing evidence target failed integrity verification")
        target.chmod(0o600)
    else:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return target, digest


def record_chart_analysis(
    db: Session,
    *,
    scope: RequestScope,
    image_bytes: bytes,
    content_type: str,
    evidence_directory: Path,
    analysis: ChartAnalysis,
    provider: ModelProvider,
    model: str | None = None,
    policy_hash: str,
    prompt: str,
    source: str,
    market_time: datetime | None,
    instrument: str | None,
    venue: str | None,
    timeframe: str | None,
    trade_plan_id: uuid.UUID | None = None,
) -> tuple[EvidenceItem, AnalysisRun]:
    validate_scope(db, scope)
    if trade_plan_id is not None:
        plan = db.scalar(
            select(TradePlan).where(
                TradePlan.workspace_id == scope.workspace_id,
                TradePlan.account_id == scope.account_id,
                TradePlan.id == trade_plan_id,
            )
        )
        if plan is None:
            raise LookupError("trade plan was not found in the requested account")
    scoped_directory = _scoped_evidence_directory(evidence_directory, scope)
    path, digest = store_evidence_file(
        image_bytes,
        content_type,
        scoped_directory,
    )
    retrieved_at = datetime.now(UTC)
    storage_uri = path.as_uri()
    evidence = db.scalar(
        select(EvidenceItem).where(
            EvidenceItem.workspace_id == scope.workspace_id,
            EvidenceItem.account_id == scope.account_id,
            EvidenceItem.sha256 == digest,
            EvidenceItem.storage_uri == storage_uri,
        )
    )
    if evidence is None:
        evidence = EvidenceItem(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            trade_plan_id=trade_plan_id,
            evidence_type="chart",
            storage_uri=storage_uri,
            sha256=digest,
            mime_type=content_type,
            source=source,
            market_time=market_time,
            retrieved_at=retrieved_at,
            metadata_json={
                "instrument": instrument,
                "venue": venue,
                "timeframe": timeframe,
            },
        )
        db.add(evidence)
        db.flush()

    output = analysis.model_dump(mode="json")
    output_bytes = json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
    run = AnalysisRun(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        evidence_id=evidence.id,
        trade_plan_id=trade_plan_id,
        analysis_type="chart",
        status="completed",
        provider=provider.name,
        model=model or provider.model,
        policy_hash=policy_hash,
        prompt_hash=_sha256(prompt.encode()),
        input_hash=digest,
        output_hash=_sha256(output_bytes),
        output_json=output,
    )
    db.add(run)
    db.add_all(
        [
            Observation(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                trade_plan_id=trade_plan_id,
                evidence_id=evidence.id,
                kind="fact",
                text=value,
                actor_type="agent",
                observed_at=market_time,
            )
            for value in analysis.visible_facts
        ]
        + [
            Observation(
                workspace_id=scope.workspace_id,
                account_id=scope.account_id,
                trade_plan_id=trade_plan_id,
                evidence_id=evidence.id,
                kind="hypothesis",
                text=value,
                actor_type="agent",
                observed_at=market_time,
            )
            for value in analysis.context_hypotheses + analysis.trigger_hypotheses
        ]
    )
    db.commit()
    db.refresh(evidence)
    db.refresh(run)
    return evidence, run
