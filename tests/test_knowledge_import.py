import json
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.models import StrategyKnowledgeItem
from app.services.catalog import create_playbook_version
from app.services.knowledge_import import (
    MAX_ITEM_CHARACTERS,
    ImportedRecord,
    _clean_pasted_content,
    _json_records,
    _path_records,
    _record_parts,
    _records_from_bytes,
    import_knowledge_text,
)
from app.services.strategy_workspace import (
    knowledge_item_reference,
    resolve_strategy_knowledge_reference,
    search_strategy_knowledge,
    search_strategy_knowledge_for_management,
    set_active_strategy_knowledge_excluded,
    set_strategy_knowledge_excluded,
)
from app.services.workspaces import RequestScope

TEST_SCOPE = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())


def test_discord_message_export_is_normalized() -> None:
    records = _json_records(
        {
            "messages": [
                {
                    "ID": "123",
                    "Timestamp": "2023-10-03T12:58:00-04:00",
                    "Contents": "Price swept equal highs and rejected.",
                    "Attachments": ["chart.png"],
                }
            ]
        },
        "discord/messages.json",
    )

    assert len(records) == 1
    assert records[0].content == "Price swept equal highs and rejected."
    assert records[0].source_reference.endswith("#message-123")
    assert records[0].metadata == {"attachments": ["chart.png"]}


def test_nested_telegram_export_is_normalized() -> None:
    records = _json_records(
        {
            "chats": {
                "list": [
                    {
                        "name": "Trading Notes",
                        "messages": [
                            {
                                "id": 77,
                                "date": "2024-01-02T14:00:00",
                                "from": "Kyle",
                                "text": [
                                    "Spring below support ",
                                    {"type": "bold", "text": "then reclaim"},
                                ],
                            }
                        ],
                    }
                ]
            }
        },
        "telegram/result.json",
    )

    assert len(records) == 1
    assert records[0].content == "Spring below support then reclaim"
    assert records[0].author == "Kyle"
    assert records[0].occurred_at is not None


def test_x_javascript_archive_is_normalized() -> None:
    payload = (
        'window.YTD.tweets.part0 = [{"tweet":{"id_str":"9",'
        '"created_at":"2024-01-02T14:00:00Z",'
        '"full_text":"Weekly gold outlook with conditional invalidation."}}];'
    )

    records = _records_from_bytes(payload.encode(), ".js", "tweets.js")

    assert len(records) == 1
    assert records[0].source_reference.endswith("#message-9")
    assert records[0].content.startswith("Weekly gold outlook")


def test_archive_type_detection_supports_telegram_and_generic(tmp_path) -> None:
    telegram = tmp_path / "telegram-export.zip"
    with zipfile.ZipFile(telegram, "w") as archive:
        archive.writestr(
            "Telegram Desktop/result.json",
            json.dumps({"messages": [{"id": 1, "text": "Test note"}]}),
        )

    records, _, source_type = _path_records(telegram)

    assert len(records) == 1
    assert source_type == "telegram"


def test_import_rejects_top_level_symlink(tmp_path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("Do not follow this link.")
    link = tmp_path / "linked-notes.md"
    link.symlink_to(source)

    with pytest.raises(ValueError, match="does not follow symlinks"):
        _path_records(link)


def test_json_import_rejects_excessive_nesting() -> None:
    payload: dict = {"content": "deep"}
    for _ in range(102):
        payload = {"nested": payload}

    with pytest.raises(ValueError, match="nesting depth"):
        _json_records(payload, "deep.json")


def test_archive_member_count_and_directory_total_size_are_bounded(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("app.services.knowledge_import.MAX_ARCHIVE_MEMBERS", 1)
    archive_path = tmp_path / "too-many.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("one.txt", "one")
        archive.writestr("two.txt", "two")
    with pytest.raises(ValueError, match="members"):
        _path_records(archive_path)

    monkeypatch.setattr("app.services.knowledge_import.MAX_DIRECTORY_BYTES", 4)
    directory = tmp_path / "large-directory"
    directory.mkdir()
    (directory / "one.txt").write_text("123")
    (directory / "two.txt").write_text("456")
    with pytest.raises(ValueError, match="250 MB"):
        _path_records(directory)


def test_single_files_and_archives_are_rejected_before_unbounded_reads(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("app.services.knowledge_import.MAX_FILE_BYTES", 4)
    source = tmp_path / "oversized.txt"
    source.write_bytes(b"12345")
    with pytest.raises(ValueError, match="20 MB"):
        _path_records(source)

    monkeypatch.setattr("app.services.knowledge_import.MAX_ARCHIVE_BYTES", 4)
    archive = tmp_path / "oversized.zip"
    archive.write_bytes(b"12345")
    with pytest.raises(ValueError, match="100 MB"):
        _path_records(archive)


def test_pasted_knowledge_is_chunked_without_silent_character_truncation() -> None:
    text = "a" * 25_000
    assert _clean_pasted_content(text) == text


def test_long_message_records_are_split_without_silent_truncation() -> None:
    content = "a" * (MAX_ITEM_CHARACTERS + 123)

    parts = _record_parts(
        ImportedRecord(
            content=content,
            source_reference="discord.json#message-1",
            kind="message",
            author="Kyle",
            metadata={"attachments": ["chart.png"]},
        )
    )

    assert "".join(part.content for part in parts) == content
    assert [part.source_reference for part in parts] == [
        "discord.json#message-1#part-1",
        "discord.json#message-1#part-2",
    ]
    assert all(part.author == "Kyle" for part in parts)
    assert all(part.metadata == {"attachments": ["chart.png"]} for part in parts)


class CapturingSession:
    def __init__(self) -> None:
        self.statement = None

    def scalars(self, statement):
        self.statement = statement
        return []


def test_strategy_search_always_filters_exact_version_and_exclusions(
    monkeypatch,
) -> None:
    strategy_version = uuid.uuid4()
    db = CapturingSession()
    monkeypatch.setattr(
        "app.services.strategy_workspace.validate_strategy_scope",
        lambda *args, **kwargs: None,
    )

    assert search_strategy_knowledge(
        db,
        strategy_version,
        "spring reclaim",
        scope=TEST_SCOPE,
    ) == []

    compiled = db.statement.compile()
    assert strategy_version in compiled.params.values()
    assert TEST_SCOPE.workspace_id in compiled.params.values()
    sql = str(compiled)
    assert "strategy_knowledge_items.playbook_version_id" in sql
    assert "strategy_knowledge_items.excluded" in sql


def test_knowledge_item_can_be_quarantined_and_restored(
    db_session,
    request_scope,
) -> None:
    name = f"knowledge-quarantine-{uuid.uuid4().hex[:10]}"
    version = create_playbook_version(
        db_session,
        workspace_id=request_scope.workspace_id,
        name=name,
        definition={"methodology": "wyckoff"},
    )
    import_knowledge_text(
        db_session,
        "Spring below support followed by a reclaim.",
        name,
        "test-note",
        scope=request_scope,
    )
    item_id = db_session.scalar(
        select(StrategyKnowledgeItem.id).where(
            StrategyKnowledgeItem.playbook_version_id == version.id
        )
    )
    assert item_id is not None

    item = set_strategy_knowledge_excluded(
        db_session,
        name,
        item_id,
        scope=request_scope,
        excluded=True,
    )
    assert item.excluded is True
    assert search_strategy_knowledge(
        db_session,
        version.id,
        "spring",
        scope=request_scope,
    ) == []

    restored = set_strategy_knowledge_excluded(
        db_session,
        name,
        item_id,
        scope=request_scope,
        excluded=False,
    )
    assert restored.excluded is False
    assert [item.id for item in search_strategy_knowledge(
        db_session,
        version.id,
        "spring",
        scope=request_scope,
    )] == [
        item_id
    ]


def test_human_reference_management_is_version_scoped_and_reversible(
    db_session,
    request_scope,
) -> None:
    strategy = f"knowledge-reference-{uuid.uuid4().hex[:10]}"
    version = create_playbook_version(
        db_session,
        workspace_id=request_scope.workspace_id,
        name=strategy,
        definition={"methodology": "wyckoff"},
    )
    import_knowledge_text(
        db_session,
        "An ICT fair value gap note that does not belong in this workspace.",
        strategy,
        "discord-export",
        scope=request_scope,
    )
    candidates = search_strategy_knowledge_for_management(
        db_session,
        version.id,
        "ICT fair value",
        scope=request_scope,
        status="active",
    )
    assert len(candidates) == 1
    reference = knowledge_item_reference(candidates[0])
    assert reference.startswith("knowledge-")
    assert str(candidates[0].id) not in reference
    assert resolve_strategy_knowledge_reference(
        db_session,
        version.id,
        reference,
        scope=request_scope,
    ).id == candidates[0].id

    quarantined = set_active_strategy_knowledge_excluded(
        db_session,
        version.id,
        reference,
        scope=request_scope,
        excluded=True,
    )
    assert quarantined.excluded is True
    assert search_strategy_knowledge_for_management(
        db_session,
        version.id,
        "ICT fair value",
        scope=request_scope,
        status="active",
    ) == []
    assert [
        item.id
        for item in search_strategy_knowledge_for_management(
            db_session,
            version.id,
            "ICT fair value",
            scope=request_scope,
            status="quarantined",
        )
    ] == [quarantined.id]

    restored = set_active_strategy_knowledge_excluded(
        db_session,
        version.id,
        reference,
        scope=request_scope,
        excluded=False,
    )
    assert restored.excluded is False


def test_human_reference_cannot_cross_strategy_versions(
    db_session,
    request_scope,
) -> None:
    first = f"knowledge-first-{uuid.uuid4().hex[:10]}"
    second = f"knowledge-second-{uuid.uuid4().hex[:10]}"
    first_version = create_playbook_version(
        db_session,
        workspace_id=request_scope.workspace_id,
        name=first,
        definition={"methodology": "wyckoff"},
    )
    second_version = create_playbook_version(
        db_session,
        workspace_id=request_scope.workspace_id,
        name=second,
        definition={"methodology": "ict"},
    )
    import_knowledge_text(
        db_session,
        "First strategy note.",
        first,
        "first.txt",
        scope=request_scope,
    )
    first_item = search_strategy_knowledge_for_management(
        db_session,
        first_version.id,
        "strategy note",
        scope=request_scope,
        status="active",
    )[0]

    with pytest.raises(LookupError, match="active strategy"):
        resolve_strategy_knowledge_reference(
            db_session,
            second_version.id,
            knowledge_item_reference(first_item),
            scope=request_scope,
        )
