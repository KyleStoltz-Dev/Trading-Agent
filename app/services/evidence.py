import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalysisRun, EvidenceItem, Observation
from app.providers import ModelProvider
from app.schemas import ChartAnalysis

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def store_evidence_file(
    data: bytes,
    content_type: str,
    directory: Path,
) -> tuple[Path, str]:
    extension = EXTENSIONS.get(content_type)
    if extension is None:
        raise ValueError("unsupported evidence content type")
    digest = _sha256(data)
    root = directory.expanduser().resolve()
    target_directory = root / digest[:2]
    target_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_directory / f"{digest}{extension}"
    if not target.exists():
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
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
    image_bytes: bytes,
    content_type: str,
    evidence_directory: Path,
    analysis: ChartAnalysis,
    provider: ModelProvider,
    policy_hash: str,
    prompt: str,
    source: str,
    market_time: datetime | None,
    instrument: str | None,
    venue: str | None,
    timeframe: str | None,
    trade_plan_id: uuid.UUID | None = None,
) -> tuple[EvidenceItem, AnalysisRun]:
    path, digest = store_evidence_file(
        image_bytes,
        content_type,
        evidence_directory,
    )
    retrieved_at = datetime.now(UTC)
    storage_uri = path.as_uri()
    evidence = db.scalar(
        select(EvidenceItem).where(
            EvidenceItem.sha256 == digest,
            EvidenceItem.storage_uri == storage_uri,
        )
    )
    if evidence is None:
        evidence = EvidenceItem(
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
        evidence_id=evidence.id,
        trade_plan_id=trade_plan_id,
        analysis_type="chart",
        status="completed",
        provider=provider.name,
        model=provider.model,
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
