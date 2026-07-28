import csv
import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnowledgeImport, StrategyKnowledgeItem
from app.schemas import KnowledgeImportResult
from app.services.strategy_workspace import resolve_strategy_version
from app.services.workspaces import RequestScope

SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".json", ".jsonl", ".csv", ".js"})
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_DIRECTORY_BYTES = 250 * 1024 * 1024
MAX_IMPORT_ITEMS = 50_000
MAX_ITEM_CHARACTERS = 20_000
MAX_METADATA_CHARACTERS = 2_000
MAX_ATTACHMENTS_PER_ITEM = 50
MAX_JSON_DEPTH = 100
MAX_JSON_NODES = 250_000
TEXT_CHUNK_CHARACTERS = 4_000


@dataclass(frozen=True)
class ImportedRecord:
    content: str
    source_reference: str
    kind: str = "document"
    author: str | None = None
    occurred_at: datetime | None = None
    metadata: dict | None = None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _clean_content(value: Any) -> str:
    if isinstance(value, list):
        value = "".join(
            item
            if isinstance(item, str)
            else str(item.get("text") or "")
            if isinstance(item, dict)
            else ""
            for item in value
        )
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()


def _clean_pasted_content(value: str) -> str:
    cleaned = value.replace("\x00", "").strip()
    if len(cleaned.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("pasted knowledge exceeds 20 MB")
    return cleaned


def _bounded_attachment_metadata(value: Any) -> list[str | dict[str, str]]:
    """Retain useful attachment labels without persisting arbitrary nested exports."""
    if not isinstance(value, list):
        return []
    bounded: list[str | dict[str, str]] = []
    for item in value[:MAX_ATTACHMENTS_PER_ITEM]:
        if isinstance(item, str) and item.strip():
            bounded.append(
                item.replace("\x00", "").strip()[:MAX_METADATA_CHARACTERS]
            )
            continue
        if not isinstance(item, dict):
            continue
        safe: dict[str, str] = {}
        for key in ("id", "filename", "fileName", "url", "content_type", "contentType"):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                safe[key] = (
                    raw.replace("\x00", "").strip()[:MAX_METADATA_CHARACTERS]
                )
        if safe:
            bounded.append(safe)
    return bounded


def _chunks(text: str, source_reference: str) -> list[ImportedRecord]:
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip()
        if len(candidate) <= TEXT_CHUNK_CHARACTERS:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > TEXT_CHUNK_CHARACTERS:
            chunks.append(paragraph[:TEXT_CHUNK_CHARACTERS])
            paragraph = paragraph[TEXT_CHUNK_CHARACTERS:]
        current = paragraph
    if current:
        chunks.append(current)
    return [
        ImportedRecord(
            content=chunk,
            source_reference=f"{source_reference}#chunk-{index}",
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def _record_parts(record: ImportedRecord) -> list[ImportedRecord]:
    """Bound retrieved rows without silently discarding long imported messages."""
    content = _clean_content(record.content)
    if not content:
        return []
    if len(content) <= MAX_ITEM_CHARACTERS:
        return [replace(record, content=content)]
    return [
        replace(
            record,
            content=content[offset : offset + MAX_ITEM_CHARACTERS],
            source_reference=(
                f"{record.source_reference}#part-"
                f"{offset // MAX_ITEM_CHARACTERS + 1}"
            ),
        )
        for offset in range(0, len(content), MAX_ITEM_CHARACTERS)
    ]


def _message_record(item: dict[str, Any], source_reference: str) -> ImportedRecord | None:
    content = _clean_content(
        item.get("content")
        or item.get("Content")
        or item.get("contents")
        or item.get("Contents")
        or item.get("message")
        or item.get("text")
        or item.get("full_text")
    )
    if not content:
        return None
    author_value = item.get("author") or item.get("Author") or item.get("from")
    if isinstance(author_value, dict):
        author = (
            author_value.get("displayName")
            or author_value.get("nickname")
            or author_value.get("name")
            or author_value.get("username")
        )
    else:
        author = author_value
    message_id = (
        item.get("id")
        or item.get("ID")
        or item.get("messageId")
        or item.get("id_str")
    )
    attachments = item.get("attachments") or item.get("Attachments") or []
    return ImportedRecord(
        content=content,
        source_reference=(
            f"{source_reference}#message-{message_id}"
            if message_id
            else source_reference
        ),
        kind="message",
        author=_clean_content(author)[:160] if isinstance(author, str) else None,
        occurred_at=_timestamp(
            item.get("timestamp")
            or item.get("Timestamp")
            or item.get("date")
            or item.get("created_at")
            or item.get("createdAt")
        ),
        metadata={"attachments": _bounded_attachment_metadata(attachments)},
    )


def _message_dicts(payload: Any) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > MAX_JSON_NODES:
            raise ValueError("JSON import exceeds the safe structure limit")
        if depth > MAX_JSON_DEPTH:
            raise ValueError("JSON import exceeds the safe nesting depth")
        if isinstance(value, list):
            stack.extend((item, depth + 1) for item in reversed(value))
            continue
        if not isinstance(value, dict):
            continue
        if any(
            key in value
            for key in (
                "content",
                "Content",
                "Contents",
                "message",
                "text",
                "full_text",
            )
        ):
            values.append(value)
            if len(values) > MAX_IMPORT_ITEMS:
                raise ValueError("knowledge import exceeds 50,000 items")
            continue
        for nested in value.values():
            if isinstance(nested, dict | list):
                stack.append((nested, depth + 1))
    return values


def _json_records(payload: Any, source_reference: str) -> list[ImportedRecord]:
    if not isinstance(payload, dict | list):
        raise ValueError("JSON import must contain an object or list")
    values = _message_dicts(payload)
    records = []
    for index, item in enumerate(values, start=1):
        record = _message_record(item, f"{source_reference}#row-{index}")
        if record is not None:
            records.append(record)
    return records


def _csv_records(text: str, source_reference: str) -> list[ImportedRecord]:
    reader = csv.DictReader(io.StringIO(text))
    records: list[ImportedRecord] = []
    for index, item in enumerate(reader, start=1):
        if index > MAX_IMPORT_ITEMS:
            raise ValueError("knowledge import exceeds 50,000 items")
        record = _message_record(dict(item), f"{source_reference}#row-{index}")
        if record is not None:
            records.append(record)
    return records


def _decode(data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("import file exceeds 20 MB")
    return data.decode("utf-8-sig")


def _read_bounded_file(path: Path, maximum_bytes: int, error: str) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("knowledge import path must be a regular file")
        if file_stat.st_size > maximum_bytes:
            raise ValueError(error)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(maximum_bytes + 1)
        if len(data) > maximum_bytes:
            raise ValueError(error)
        return data
    finally:
        os.close(descriptor)


def _records_from_bytes(
    data: bytes,
    suffix: str,
    source_reference: str,
) -> list[ImportedRecord]:
    text = _decode(data)
    if suffix in {".txt", ".md"}:
        return _chunks(text, source_reference)
    if suffix in {".json", ".js"}:
        if suffix == ".js" and not text.lstrip().startswith(("[", "{")):
            marker = text.find("=")
            if marker == -1:
                raise ValueError("JavaScript archive does not contain JSON data")
            text = text[marker + 1 :].strip().removesuffix(";")
        return _json_records(json.loads(text), source_reference)
    if suffix == ".jsonl":
        records: list[ImportedRecord] = []
        parsed_lines = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            parsed_lines += 1
            if parsed_lines > MAX_IMPORT_ITEMS:
                raise ValueError("knowledge import exceeds 50,000 items")
            payload = json.loads(line)
            line_records = _json_records(
                payload,
                f"{source_reference}#line-{line_number}",
            )
            if len(records) + len(line_records) > MAX_IMPORT_ITEMS:
                raise ValueError("knowledge import exceeds 50,000 items")
            records.extend(line_records)
        return records
    if suffix == ".csv":
        return _csv_records(text, source_reference)
    raise ValueError(f"unsupported import type: {suffix}")


def _path_records(path: Path) -> tuple[list[ImportedRecord], bytes, str]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError("knowledge import does not follow symlinks")
    resolved = expanded.resolve()
    if resolved.is_file() and resolved.suffix.lower() == ".zip":
        archive_data = _read_bounded_file(
            resolved,
            MAX_ARCHIVE_BYTES,
            "knowledge archive exceeds 100 MB",
        )
        records: list[ImportedRecord] = []
        total_size = 0
        member_names: list[str] = []
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("knowledge archive exceeds 10,000 members")
            for member in members:
                virtual = PurePosixPath(member.filename)
                if (
                    member.is_dir()
                    or virtual.is_absolute()
                    or ".." in virtual.parts
                    or Path(member.filename).suffix.lower() not in SUPPORTED_SUFFIXES
                ):
                    continue
                total_size += member.file_size
                if member.file_size > MAX_FILE_BYTES or total_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("knowledge archive exceeds safe expanded-size limits")
                member_data = archive.read(member)
                member_names.append(member.filename.lower())
                records.extend(
                    _records_from_bytes(
                        member_data,
                        Path(member.filename).suffix.lower(),
                        f"{resolved.name}:{member.filename}",
                    )
                )
                if len(records) > MAX_IMPORT_ITEMS:
                    raise ValueError("knowledge import exceeds 50,000 items")
        joined_names = "\n".join(member_names)
        if "telegram" in joined_names or "result.json" in joined_names:
            source_type = "telegram"
        elif "twitter" in joined_names or "tweets.js" in joined_names:
            source_type = "x"
        elif "discord" in joined_names or "/messages/" in f"/{joined_names}":
            source_type = "discord"
        else:
            source_type = "generic"
        return records, archive_data, source_type
    if resolved.is_file():
        suffix = resolved.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(
                "supported imports are TXT, Markdown, JSON, CSV, Discord ZIP, or a directory"
            )
        data = _read_bounded_file(
            resolved,
            MAX_FILE_BYTES,
            "import file exceeds 20 MB",
        )
        records = _records_from_bytes(data, suffix, resolved.name)
        lowered_name = resolved.name.lower()
        if "telegram" in lowered_name or lowered_name == "result.json":
            source_type = "telegram"
        elif "tweet" in lowered_name or "twitter" in lowered_name:
            source_type = "x"
        elif "discord" in lowered_name or "message" in lowered_name:
            source_type = "discord"
        else:
            source_type = "file" if suffix in {".txt", ".md"} else "generic"
        return records, data, source_type
    if resolved.is_dir():
        digest = hashlib.sha256()
        records = []
        files = [
            item
            for item in sorted(resolved.rglob("*"))
            if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
        ]
        if len(files) > 5_000:
            raise ValueError("knowledge directory exceeds 5,000 supported files")
        total_size = 0
        for item in files:
            item_resolved = item.resolve()
            if item.is_symlink() or not item_resolved.is_relative_to(resolved):
                continue
            total_size += item.stat().st_size
            if total_size > MAX_DIRECTORY_BYTES:
                raise ValueError("knowledge directory exceeds 250 MB")
            data = _read_bounded_file(
                item_resolved,
                MAX_FILE_BYTES,
                "import file exceeds 20 MB",
            )
            relative = item.relative_to(resolved).as_posix()
            digest.update(relative.encode())
            digest.update(hashlib.sha256(data).digest())
            records.extend(
                _records_from_bytes(data, item.suffix.lower(), relative)
            )
            if len(records) > MAX_IMPORT_ITEMS:
                raise ValueError("knowledge import exceeds 50,000 items")
        return records, digest.digest(), "directory"
    raise FileNotFoundError(f"knowledge import path was not found: {resolved}")


def _persist_records(
    db: Session,
    *,
    scope: RequestScope,
    strategy: str,
    source_name: str,
    source_locator: str | None,
    source_type: str,
    source_hash: str,
    records: list[ImportedRecord],
) -> KnowledgeImportResult:
    playbook, version = resolve_strategy_version(db, strategy, scope=scope)
    existing = db.scalar(
        select(KnowledgeImport).where(
            KnowledgeImport.workspace_id == scope.workspace_id,
            KnowledgeImport.playbook_version_id == version.id,
            KnowledgeImport.source_hash == source_hash,
        )
    )
    if existing is not None:
        return KnowledgeImportResult(
            import_id=existing.id,
            strategy=playbook.name,
            strategy_version=version.version,
            source_name=existing.source_name,
            source_type=existing.source_type,
            imported=existing.item_count,
            skipped=existing.skipped_count,
        )
    knowledge_import = KnowledgeImport(
        workspace_id=scope.workspace_id,
        playbook_version_id=version.id,
        source_type=source_type,
        source_name=source_name[:255],
        source_locator=source_locator,
        source_hash=source_hash,
        status="completed",
    )
    db.add(knowledge_import)
    db.flush()
    existing_hashes = set(
        db.scalars(
            select(StrategyKnowledgeItem.content_hash).where(
                StrategyKnowledgeItem.workspace_id == scope.workspace_id,
                StrategyKnowledgeItem.playbook_version_id == version.id
            )
        )
    )
    imported = 0
    skipped = 0
    item_count = 0
    for record in records:
        parts = _record_parts(record)
        if not parts:
            skipped += 1
            continue
        for part in parts:
            item_count += 1
            if item_count > MAX_IMPORT_ITEMS:
                raise ValueError("knowledge import exceeds 50,000 items after safe chunking")
            content_hash = hashlib.sha256(part.content.encode()).hexdigest()
            if content_hash in existing_hashes:
                skipped += 1
                continue
            db.add(
                StrategyKnowledgeItem(
                    workspace_id=scope.workspace_id,
                    import_id=knowledge_import.id,
                    playbook_version_id=version.id,
                    kind=part.kind,
                    source_reference=part.source_reference,
                    author=part.author,
                    occurred_at=part.occurred_at,
                    content=part.content,
                    content_hash=content_hash,
                    metadata_json=part.metadata or {},
                )
            )
            existing_hashes.add(content_hash)
            imported += 1
    knowledge_import.item_count = imported
    knowledge_import.skipped_count = skipped
    knowledge_import.status = "partial" if skipped else "completed"
    db.commit()
    db.refresh(knowledge_import)
    return KnowledgeImportResult(
        import_id=knowledge_import.id,
        strategy=playbook.name,
        strategy_version=version.version,
        source_name=knowledge_import.source_name,
        source_type=source_type,
        imported=imported,
        skipped=skipped,
    )


def import_knowledge_path(
    db: Session,
    path: Path,
    strategy: str,
    *,
    scope: RequestScope,
) -> KnowledgeImportResult:
    expanded = path.expanduser()
    records, source_bytes, source_type = _path_records(expanded)
    resolved = expanded.resolve()
    return _persist_records(
        db,
        scope=scope,
        strategy=strategy,
        source_name=resolved.name,
        source_locator=str(resolved),
        source_type=source_type,
        source_hash=hashlib.sha256(source_bytes).hexdigest(),
        records=records,
    )


def import_knowledge_text(
    db: Session,
    text: str,
    strategy: str,
    name: str = "pasted-notes",
    *,
    scope: RequestScope,
) -> KnowledgeImportResult:
    cleaned = _clean_pasted_content(text)
    if not cleaned:
        raise ValueError("pasted knowledge cannot be empty")
    return _persist_records(
        db,
        scope=scope,
        strategy=strategy,
        source_name=name,
        source_locator=None,
        source_type="paste",
        source_hash=hashlib.sha256(cleaned.encode()).hexdigest(),
        records=_chunks(cleaned, name),
    )
