import uuid

import pytest

from app.services.catalog import create_playbook_version
from app.services.conversations import (
    add_turn,
    conversation_history,
    conversation_transcript,
    create_conversation,
    normalize_session_name,
)
from app.services.strategy_workspace import set_session_strategy


def test_session_names_are_predictable_slugs() -> None:
    assert normalize_session_name("Gold NY Review") == "gold-ny-review"
    assert normalize_session_name("Daily 2026/07/23") == "daily-2026-07-23"


def test_empty_session_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="letters or numbers"):
        normalize_session_name("***")


def test_switching_from_wyckoff_to_ict_does_not_leak_history(db_session) -> None:
    suffix = uuid.uuid4().hex[:10]
    wyckoff_name = f"wyckoff-{suffix}"
    ict_name = f"ict-{suffix}"
    wyckoff = create_playbook_version(
        db_session,
        name=wyckoff_name,
        definition={"entry": "spring and reclaim"},
    )
    ict = create_playbook_version(
        db_session,
        name=ict_name,
        definition={"entry": "liquidity sweep and displacement"},
    )
    conversation = create_conversation(db_session, name=f"isolation-{suffix}")

    set_session_strategy(db_session, conversation, wyckoff_name)
    add_turn(
        db_session,
        conversation,
        "user",
        "Use the spring as my Wyckoff trigger.",
        playbook_version_id=wyckoff.id,
    )
    add_turn(
        db_session,
        conversation,
        "assistant",
        "Wyckoff-only response.",
        playbook_version_id=wyckoff.id,
    )

    set_session_strategy(db_session, conversation, ict_name)
    assert conversation_history(
        db_session,
        conversation,
        playbook_version_id=ict.id,
    ) == []

    add_turn(
        db_session,
        conversation,
        "user",
        "Use only the active ICT definition.",
        playbook_version_id=ict.id,
    )
    ict_history = conversation_history(
        db_session,
        conversation,
        playbook_version_id=ict.id,
    )
    assert ict_history == [
        {"role": "user", "content": "Use only the active ICT definition."}
    ]
    assert all("Wyckoff" not in turn["content"] for turn in ict_history)

    set_session_strategy(db_session, conversation, None)
    add_turn(
        db_session,
        conversation,
        "user",
        "This is a safely general journal note.",
        playbook_version_id=None,
    )
    assert conversation_history(
        db_session,
        conversation,
        playbook_version_id=None,
    ) == [
        {"role": "user", "content": "This is a safely general journal note."}
    ]

    transcript = conversation_transcript(db_session, conversation)
    assert len(transcript) == 4
    assert any("Wyckoff" in turn["content"] for turn in transcript)
    assert any("ICT" in turn["content"] for turn in transcript)
