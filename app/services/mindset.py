import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MindsetCheckIn, PlaybookVersion, TradePlan
from app.schemas import MindsetCheckInCreate, MindsetCheckInRead
from app.services.journal import get_trade_plan


def mindset_read(
    check_in: MindsetCheckIn,
    trade_reference: str | None = None,
) -> MindsetCheckInRead:
    return MindsetCheckInRead(
        id=check_in.id,
        playbook_version_id=check_in.playbook_version_id,
        trade_plan_id=check_in.trade_plan_id,
        trade_reference=trade_reference,
        phase=check_in.phase,
        readiness=check_in.readiness,
        accepted_risk=check_in.accepted_risk,
        emotion_tags=check_in.emotion_tags,
        note=check_in.note,
        created_at=check_in.created_at,
    )


def create_mindset_check_in(
    db: Session,
    request: MindsetCheckInCreate,
    *,
    playbook_version_id: uuid.UUID,
    commit: bool = True,
) -> MindsetCheckInRead:
    if db.get(PlaybookVersion, playbook_version_id) is None:
        raise LookupError(
            f"strategy version was not found: {playbook_version_id}"
        )
    trade = (
        get_trade_plan(
            db,
            request.trade_reference,
            playbook_version_id=playbook_version_id,
        )
        if request.trade_reference is not None
        else None
    )
    check_in = MindsetCheckIn(
        playbook_version_id=playbook_version_id,
        trade_plan_id=trade.id if trade else None,
        **request.model_dump(exclude={"trade_reference"}),
    )
    db.add(check_in)
    db.flush()
    if commit:
        db.commit()
        db.refresh(check_in)
    return mindset_read(check_in, trade.reference if trade else None)


def list_mindset_check_ins(
    db: Session,
    *,
    playbook_version_id: uuid.UUID,
    limit: int = 20,
    phase: str | None = None,
) -> list[MindsetCheckInRead]:
    statement = (
        select(MindsetCheckIn, TradePlan.reference)
        .outerjoin(TradePlan, TradePlan.id == MindsetCheckIn.trade_plan_id)
        .where(MindsetCheckIn.playbook_version_id == playbook_version_id)
        .order_by(MindsetCheckIn.created_at.desc())
        .limit(limit)
    )
    if phase is not None:
        statement = statement.where(MindsetCheckIn.phase == phase)
    return [
        mindset_read(check_in, trade_reference)
        for check_in, trade_reference in db.execute(statement)
    ]
