import json
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.models import StrategyKnowledgeItem
from app.services.catalog import create_playbook_version
from app.services.knowledge_import import (
    _json_records,
    _path_records,
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


class CapturingSession:
    def __init__(self) -> None:
        self.statement = None

    def scalars(self, statement):
        self.statement = statement
        return []


def test_strategy_search_always_filters_exact_version_and_exclusions() -> None:
    strategy_version = uuid.uuid4()
    db = CapturingSession()

    assert search_strategy_knowledge(db, strategy_version, "spring reclaim") == []

    compiled = db.statement.compile()
    assert strategy_version in compiled.params.values()
    sql = str(compiled)
    assert "strategy_knowledge_items.playbook_version_id" in sql
    assert "strategy_knowledge_items.excluded" in sql


def test_knowledge_item_can_be_quarantined_and_restored(db_session) -> None:
    name = f"knowledge-quarantine-{uuid.uuid4().hex[:10]}"
    version = create_playbook_version(
        db_session,
        name=name,
        definition={"methodology": "wyckoff"},
    )
    import_knowledge_text(
        db_session,
        "Spring below support followed by a reclaim.",
        name,
        "test-note",
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
        excluded=True,
    )
    assert item.excluded is True
    assert search_strategy_knowledge(db_session, version.id, "spring") == []

    restored = set_strategy_knowledge_excluded(
        db_session,
        name,
        item_id,
        excluded=False,
    )
    assert restored.excluded is False
    assert [item.id for item in search_strategy_knowledge(db_session, version.id, "spring")] == [
        item_id
    ]


def test_human_reference_management_is_version_scoped_and_reversible(db_session) -> None:
    strategy = f"knowledge-reference-{uuid.uuid4().hex[:10]}"
    version = create_playbook_version(
        db_session,
        name=strategy,
        definition={"methodology": "wyckoff"},
    )
    import_knowledge_text(
        db_session,
        "An ICT fair value gap note that does not belong in this workspace.",
        strategy,
        "discord-export",
    )
    candidates = search_strategy_knowledge_for_management(
        db_session,
        version.id,
        "ICT fair value",
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
    ).id == candidates[0].id

    quarantined = set_active_strategy_knowledge_excluded(
        db_session,
        version.id,
        reference,
        excluded=True,
    )
    assert quarantined.excluded is True
    assert search_strategy_knowledge_for_management(
        db_session,
        version.id,
        "ICT fair value",
        status="active",
    ) == []
    assert [
        item.id
        for item in search_strategy_knowledge_for_management(
            db_session,
            version.id,
            "ICT fair value",
            status="quarantined",
        )
    ] == [quarantined.id]

    restored = set_active_strategy_knowledge_excluded(
        db_session,
        version.id,
        reference,
        excluded=False,
    )
    assert restored.excluded is False


def test_human_reference_cannot_cross_strategy_versions(db_session) -> None:
    first = f"knowledge-first-{uuid.uuid4().hex[:10]}"
    second = f"knowledge-second-{uuid.uuid4().hex[:10]}"
    first_version = create_playbook_version(
        db_session,
        name=first,
        definition={"methodology": "wyckoff"},
    )
    second_version = create_playbook_version(
        db_session,
        name=second,
        definition={"methodology": "ict"},
    )
    import_knowledge_text(db_session, "First strategy note.", first, "first.txt")
    first_item = search_strategy_knowledge_for_management(
        db_session,
        first_version.id,
        "strategy note",
        status="active",
    )[0]

    with pytest.raises(LookupError, match="active strategy"):
        resolve_strategy_knowledge_reference(
            db_session,
            second_version.id,
            knowledge_item_reference(first_item),
        )
