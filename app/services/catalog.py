import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    BrokerConnection,
    Instrument,
    InstrumentMapping,
    InstrumentSpecification,
    Playbook,
    PlaybookVersion,
    TradingAccount,
)
from app.schemas import InstrumentSpecificationCreate


def canonical_symbol(value: str) -> str:
    normalized = "".join(character for character in value.upper() if character.isalnum())
    if not normalized:
        raise ValueError("instrument symbol must contain letters or numbers")
    return normalized


def get_or_create_instrument(
    db: Session,
    symbol: str,
    *,
    asset_class: str = "unknown",
) -> Instrument:
    normalized = canonical_symbol(symbol)
    statement = select(Instrument).where(Instrument.canonical_symbol == normalized)
    if asset_class != "unknown":
        statement = statement.where(Instrument.asset_class == asset_class)
    instrument = db.scalar(statement.order_by(Instrument.created_at))
    if instrument is None:
        instrument = Instrument(
            canonical_symbol=normalized,
            asset_class=asset_class,
        )
        db.add(instrument)
        db.flush()
    return instrument


def get_or_create_mapping(
    db: Session,
    instrument: Instrument,
    *,
    provider: str,
    external_symbol: str,
    venue: str | None,
) -> InstrumentMapping:
    mapping = db.scalar(
        select(InstrumentMapping).where(
            InstrumentMapping.provider == provider,
            InstrumentMapping.external_symbol == external_symbol,
        )
    )
    if mapping is None:
        mapping = InstrumentMapping(
            instrument_id=instrument.id,
            provider=provider,
            external_symbol=external_symbol,
            venue=venue,
        )
        db.add(mapping)
        db.flush()
    elif mapping.instrument_id != instrument.id:
        raise ValueError(
            f"{provider}:{external_symbol} is already mapped to another instrument"
        )
    return mapping


def configure_instrument_specification(
    db: Session,
    request: InstrumentSpecificationCreate,
    *,
    workspace_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    retrieved_at: datetime | None = None,
) -> InstrumentSpecification:
    if (workspace_id is None) != (account_id is None):
        raise ValueError(
            "workspace_id and account_id must both be provided for an "
            "account-specific specification"
        )
    now = retrieved_at or datetime.now(UTC)
    instrument = get_or_create_instrument(
        db,
        request.canonical_symbol,
        asset_class=request.asset_class,
    )
    mapping = get_or_create_mapping(
        db,
        instrument,
        provider=request.provider,
        external_symbol=request.external_symbol,
        venue=request.venue,
    )
    previous = db.scalar(
        select(InstrumentSpecification)
        .where(
            InstrumentSpecification.instrument_mapping_id == mapping.id,
            InstrumentSpecification.workspace_id == workspace_id,
            InstrumentSpecification.account_id == account_id,
            InstrumentSpecification.effective_to.is_(None),
        )
        .order_by(InstrumentSpecification.effective_from.desc())
    )
    if previous is not None:
        previous.effective_to = now
    specification = InstrumentSpecification(
        workspace_id=workspace_id,
        instrument_mapping_id=mapping.id,
        account_id=account_id,
        contract_size=request.contract_size,
        tick_size=request.tick_size,
        tick_value_per_quantity_unit=request.tick_value_per_quantity_unit,
        minimum_quantity=request.minimum_quantity,
        maximum_quantity=request.maximum_quantity,
        quantity_step=request.quantity_step,
        margin_rate=request.margin_rate,
        estimated_spread=request.estimated_spread,
        commission_per_quantity=request.commission_per_quantity,
        financing_per_quantity_day=request.financing_per_quantity_day,
        pnl_currency=request.pnl_currency.upper(),
        effective_from=now,
        source=request.source,
        retrieved_at=now,
    )
    db.add(specification)
    db.commit()
    db.refresh(specification)
    return specification


def active_instrument_specification(
    db: Session,
    *,
    provider: str,
    external_symbol: str,
    workspace_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> InstrumentSpecification:
    if (workspace_id is None) != (account_id is None):
        raise ValueError(
            "workspace_id and account_id must both be provided for an "
            "account-specific specification"
        )
    effective_at = at or datetime.now(UTC)
    statement = (
        select(InstrumentSpecification)
        .join(InstrumentMapping)
        .where(
            InstrumentMapping.provider == provider,
            InstrumentMapping.external_symbol == external_symbol,
            InstrumentSpecification.workspace_id == workspace_id,
            InstrumentSpecification.effective_from <= effective_at,
            (
                InstrumentSpecification.effective_to.is_(None)
                | (InstrumentSpecification.effective_to > effective_at)
            ),
            (
                (InstrumentSpecification.account_id == account_id)
                if account_id is not None
                else InstrumentSpecification.account_id.is_(None)
            ),
        )
        .order_by(InstrumentSpecification.effective_from.desc())
    )
    specification = db.scalar(statement)
    if specification is None:
        raise LookupError(
            f"no active instrument specification for {provider}:{external_symbol}"
        )
    return specification


def configure_account(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    broker: str,
    external_account_id: str,
    label: str,
    currency: str,
    mode: str,
    provider: str,
    environment: str,
    config_reference: str | None,
    make_default: bool = False,
    commit: bool = True,
) -> tuple[TradingAccount, BrokerConnection]:
    account = db.scalar(
        select(TradingAccount).where(
            TradingAccount.workspace_id == workspace_id,
            TradingAccount.broker == broker,
            TradingAccount.external_account_id == external_account_id,
        )
    )
    if account is None:
        account = TradingAccount(
            workspace_id=workspace_id,
            broker=broker,
            external_account_id=external_account_id,
            label=label,
            currency=currency.upper(),
            mode=mode,
        )
        db.add(account)
        db.flush()
    else:
        account.label = label
        account.currency = currency.upper()
        account.mode = mode
        account.active = True
    connection = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.workspace_id == workspace_id,
            BrokerConnection.provider == provider,
            BrokerConnection.account_id == account.id,
        )
    )
    if connection is None:
        connection = BrokerConnection(
            workspace_id=workspace_id,
            account_id=account.id,
            provider=provider,
            environment=environment,
            config_reference=config_reference,
        )
        db.add(connection)
    else:
        changed = (
            connection.environment != environment
            or connection.config_reference != config_reference
        )
        connection.environment = environment
        connection.config_reference = config_reference
        if changed or connection.status == "disabled":
            connection.status = "configured"
    if make_default:
        db.execute(
            update(TradingAccount)
            .where(TradingAccount.workspace_id == workspace_id)
            .values(is_default=False)
        )
        account.is_default = True
    if commit:
        db.commit()
        db.refresh(account)
        db.refresh(connection)
    else:
        db.flush()
    return account, connection


def create_playbook_version(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    definition: dict,
    description: str = "",
    change_hypothesis: str | None = None,
    sample_requirement: int | None = None,
    created_by: str = "human",
) -> PlaybookVersion:
    serialized = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(serialized.encode()).hexdigest()
    playbook = db.scalar(
        select(Playbook).where(
            Playbook.workspace_id == workspace_id,
            Playbook.name == name,
        )
    )
    if playbook is None:
        playbook = Playbook(
            workspace_id=workspace_id,
            name=name,
            description=description,
        )
        db.add(playbook)
        db.flush()
    existing = db.scalar(
        select(PlaybookVersion).where(
            PlaybookVersion.workspace_id == workspace_id,
            PlaybookVersion.playbook_id == playbook.id,
            PlaybookVersion.content_hash == content_hash,
        )
    )
    if existing is not None:
        return existing
    latest = db.scalar(
        select(PlaybookVersion)
        .where(
            PlaybookVersion.workspace_id == workspace_id,
            PlaybookVersion.playbook_id == playbook.id,
        )
        .order_by(PlaybookVersion.version.desc())
    )
    version = PlaybookVersion(
        workspace_id=workspace_id,
        playbook_id=playbook.id,
        version=1 if latest is None else latest.version + 1,
        definition=definition,
        change_hypothesis=change_hypothesis,
        sample_requirement=sample_requirement,
        content_hash=content_hash,
        created_by=created_by,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def verify_playbook_version_integrity(version: PlaybookVersion) -> None:
    serialized = json.dumps(
        version.definition,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(serialized.encode()).hexdigest()
    if version.content_hash != expected:
        raise ValueError(
            "strategy version integrity check failed; stored definition does not "
            "match its immutable content hash"
        )
