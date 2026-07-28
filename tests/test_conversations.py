import hashlib
import json
import uuid
from dataclasses import FrozenInstanceError

import pytest

from app.models import Playbook, PlaybookVersion, TradingAccount, Workspace
from app.services.conversations import (
    add_turn,
    conversation_history,
    conversation_transcript,
    create_conversation,
    get_conversation,
    get_conversation_by_name,
    latest_conversation,
    list_conversations,
    normalize_session_name,
    resolve_conversation,
    update_turn_outcome,
)
from app.services.workspaces import (
    RequestScope,
    list_accounts,
    list_workspaces,
    resolve_account,
    resolve_scope,
    resolve_workspace,
)


def _workspace_scope(
    db_session,
    *,
    suffix: str,
    account_label: str = "Primary",
) -> tuple[Workspace, TradingAccount, RequestScope]:
    workspace = Workspace(
        slug=f"workspace-{suffix}",
        name=f"Workspace {suffix}",
    )
    db_session.add(workspace)
    db_session.flush()
    account = TradingAccount(
        workspace_id=workspace.id,
        broker="test",
        external_account_id=f"external-{suffix}-{account_label.lower()}",
        label=account_label,
        currency="USD",
        mode="practice",
        is_default=True,
    )
    db_session.add(account)
    db_session.commit()
    return (
        workspace,
        account,
        RequestScope(workspace_id=workspace.id, account_id=account.id),
    )


def _additional_account(
    db_session,
    workspace: Workspace,
    *,
    suffix: str,
) -> tuple[TradingAccount, RequestScope]:
    account = TradingAccount(
        workspace_id=workspace.id,
        broker="test",
        external_account_id=f"external-{suffix}",
        label=f"Account {suffix}",
        currency="USD",
        mode="practice",
        is_default=False,
    )
    db_session.add(account)
    db_session.commit()
    return (
        account,
        RequestScope(workspace_id=workspace.id, account_id=account.id),
    )


def _strategy_version(
    db_session,
    workspace: Workspace,
    *,
    suffix: str,
    definition: dict,
) -> PlaybookVersion:
    playbook = Playbook(
        workspace_id=workspace.id,
        name=f"strategy-{suffix}",
        description="Test strategy",
    )
    db_session.add(playbook)
    db_session.flush()
    serialized = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    version = PlaybookVersion(
        workspace_id=workspace.id,
        playbook_id=playbook.id,
        version=1,
        definition=definition,
        content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
        created_by="test",
    )
    db_session.add(version)
    db_session.commit()
    return version


def test_request_scope_is_immutable() -> None:
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())

    with pytest.raises(FrozenInstanceError):
        scope.account_id = uuid.uuid4()  # type: ignore[misc]


def test_session_names_are_predictable_slugs() -> None:
    assert normalize_session_name("Gold NY Review") == "gold-ny-review"
    assert normalize_session_name("Daily 2026/07/23") == "daily-2026-07-23"


def test_empty_session_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="letters or numbers"):
        normalize_session_name("***")


def test_workspace_and_account_resolvers_never_cross_workspace(db_session) -> None:
    suffix = uuid.uuid4().hex[:10]
    workspace, account, scope = _workspace_scope(db_session, suffix=f"a-{suffix}")
    other_workspace, other_account, _ = _workspace_scope(
        db_session,
        suffix=f"b-{suffix}",
    )

    assert resolve_workspace(db_session, workspace.id) is workspace
    assert resolve_workspace(db_session, workspace.slug) is workspace
    assert workspace in list_workspaces(db_session)
    assert resolve_account(db_session, workspace.id, account.id) is account
    assert resolve_account(db_session, workspace.id, account.label) is account
    assert resolve_scope(
        db_session,
        workspace_reference=workspace.slug,
        account_reference=account.label,
    ) == scope
    assert list_accounts(db_session, workspace.id) == [account]

    assert resolve_account(db_session, workspace.id, other_account.id) is None
    with pytest.raises(LookupError, match="requested workspace"):
        create_conversation(
            db_session,
            name=f"invalid-{suffix}",
            scope=RequestScope(
                workspace_id=workspace.id,
                account_id=other_account.id,
            ),
        )
    assert other_workspace.id != scope.workspace_id


def test_same_workspace_accounts_have_isolated_conversations(db_session) -> None:
    suffix = uuid.uuid4().hex[:10]
    workspace, _, primary_scope = _workspace_scope(
        db_session,
        suffix=suffix,
    )
    _, secondary_scope = _additional_account(
        db_session,
        workspace,
        suffix=suffix,
    )
    name = f"account-isolation-{suffix}"
    primary = create_conversation(db_session, name=name, scope=primary_scope)
    secondary = create_conversation(db_session, name=name, scope=secondary_scope)

    assert primary.id != secondary.id
    assert get_conversation(db_session, primary.id, scope=secondary_scope) is None
    assert (
        get_conversation_by_name(db_session, name, scope=secondary_scope)
        is secondary
    )
    assert resolve_conversation(db_session, str(primary.id), scope=secondary_scope) is None
    assert list_conversations(db_session, scope=primary_scope) == [primary]
    assert list_conversations(db_session, scope=secondary_scope) == [secondary]
    assert latest_conversation(db_session, scope=primary_scope) is primary
    assert latest_conversation(db_session, scope=secondary_scope) is secondary

    with pytest.raises(LookupError, match="conversation was not found"):
        add_turn(
            db_session,
            primary,
            "user",
            "Do not leak this turn.",
            scope=secondary_scope,
            playbook_version_id=None,
        )
    with pytest.raises(LookupError, match="conversation was not found"):
        conversation_history(
            db_session,
            primary,
            scope=secondary_scope,
            playbook_version_id=None,
        )
    with pytest.raises(LookupError, match="conversation was not found"):
        conversation_transcript(
            db_session,
            primary,
            scope=secondary_scope,
        )


def test_other_workspace_cannot_resolve_or_read_conversation(db_session) -> None:
    suffix = uuid.uuid4().hex[:10]
    _, _, first_scope = _workspace_scope(db_session, suffix=f"first-{suffix}")
    _, _, second_scope = _workspace_scope(db_session, suffix=f"second-{suffix}")
    conversation = create_conversation(
        db_session,
        name=f"private-{suffix}",
        scope=first_scope,
    )
    add_turn(
        db_session,
        conversation,
        "user",
        "Workspace-private journal content.",
        scope=first_scope,
        playbook_version_id=None,
    )

    assert get_conversation(db_session, conversation.id, scope=second_scope) is None
    assert (
        get_conversation_by_name(
            db_session,
            conversation.name,
            scope=second_scope,
        )
        is None
    )
    assert (
        resolve_conversation(
            db_session,
            str(conversation.id),
            scope=second_scope,
        )
        is None
    )
    assert conversation not in list_conversations(db_session, scope=second_scope)

    with pytest.raises(LookupError, match="conversation was not found"):
        conversation_transcript(
            db_session,
            conversation,
            scope=second_scope,
        )


def test_switching_strategies_does_not_leak_history(db_session) -> None:
    suffix = uuid.uuid4().hex[:10]
    workspace, _, scope = _workspace_scope(db_session, suffix=suffix)
    wyckoff = _strategy_version(
        db_session,
        workspace,
        suffix=f"wyckoff-{suffix}",
        definition={"entry": "spring and reclaim"},
    )
    ict = _strategy_version(
        db_session,
        workspace,
        suffix=f"ict-{suffix}",
        definition={"entry": "liquidity sweep and displacement"},
    )
    conversation = create_conversation(
        db_session,
        name=f"isolation-{suffix}",
        scope=scope,
    )

    add_turn(
        db_session,
        conversation,
        "user",
        "Use the spring as my Wyckoff trigger.",
        scope=scope,
        playbook_version_id=wyckoff.id,
    )
    add_turn(
        db_session,
        conversation,
        "assistant",
        "Wyckoff-only response.",
        scope=scope,
        playbook_version_id=wyckoff.id,
    )

    assert conversation_history(
        db_session,
        conversation,
        scope=scope,
        playbook_version_id=ict.id,
    ) == []

    add_turn(
        db_session,
        conversation,
        "user",
        "Use only the active ICT definition.",
        scope=scope,
        playbook_version_id=ict.id,
    )
    ict_history = conversation_history(
        db_session,
        conversation,
        scope=scope,
        playbook_version_id=ict.id,
    )
    assert ict_history == [
        {"role": "user", "content": "Use only the active ICT definition."}
    ]
    assert all("Wyckoff" not in turn["content"] for turn in ict_history)

    add_turn(
        db_session,
        conversation,
        "user",
        "This is a safely general journal note.",
        scope=scope,
        playbook_version_id=None,
    )
    assert conversation_history(
        db_session,
        conversation,
        scope=scope,
        playbook_version_id=None,
    ) == [
        {"role": "user", "content": "This is a safely general journal note."}
    ]

    transcript = conversation_transcript(
        db_session,
        conversation,
        scope=scope,
    )
    assert len(transcript) == 4
    assert any("Wyckoff" in turn["content"] for turn in transcript)
    assert any("ICT" in turn["content"] for turn in transcript)


def test_strategy_from_other_workspace_is_rejected(db_session) -> None:
    suffix = uuid.uuid4().hex[:10]
    first_workspace, _, first_scope = _workspace_scope(
        db_session,
        suffix=f"first-strategy-{suffix}",
    )
    second_workspace, _, _ = _workspace_scope(
        db_session,
        suffix=f"second-strategy-{suffix}",
    )
    own_strategy = _strategy_version(
        db_session,
        first_workspace,
        suffix=f"own-{suffix}",
        definition={"entry": "reclaim"},
    )
    foreign_strategy = _strategy_version(
        db_session,
        second_workspace,
        suffix=f"foreign-{suffix}",
        definition={"entry": "breakout"},
    )
    conversation = create_conversation(
        db_session,
        name=f"strategy-scope-{suffix}",
        scope=first_scope,
    )

    add_turn(
        db_session,
        conversation,
        "user",
        "This strategy belongs here.",
        scope=first_scope,
        playbook_version_id=own_strategy.id,
    )
    with pytest.raises(LookupError, match="strategy version was not found"):
        add_turn(
            db_session,
            conversation,
            "user",
            "This strategy belongs to another workspace.",
            scope=first_scope,
            playbook_version_id=foreign_strategy.id,
        )
    with pytest.raises(LookupError, match="strategy version was not found"):
        conversation_history(
            db_session,
            conversation,
            scope=first_scope,
            playbook_version_id=foreign_strategy.id,
        )


def test_incomplete_turns_are_durable_but_not_reused_as_model_history(
    db_session,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    _, _, scope = _workspace_scope(db_session, suffix=f"outcomes-{suffix}")
    conversation = create_conversation(
        db_session,
        name=f"outcomes-{suffix}",
        scope=scope,
    )
    failed_request = uuid.uuid4()
    failed = add_turn(
        db_session,
        conversation,
        "user",
        "Attempt the requested analysis.",
        scope=scope,
        playbook_version_id=None,
        request_id=failed_request,
        status="pending",
    )
    update_turn_outcome(
        db_session,
        failed,
        scope=scope,
        status="partial",
        error_type="ProviderTimeout",
    )
    add_turn(
        db_session,
        conversation,
        "assistant",
        "The request stopped after one confirmed mutation.",
        scope=scope,
        playbook_version_id=None,
        request_id=failed_request,
        status="partial",
        error_type="ProviderTimeout",
    )
    add_turn(
        db_session,
        conversation,
        "user",
        "This completed turn is safe to reuse.",
        scope=scope,
        playbook_version_id=None,
        request_id=uuid.uuid4(),
    )

    assert conversation_history(
        db_session,
        conversation,
        scope=scope,
        playbook_version_id=None,
    ) == [
        {
            "role": "user",
            "content": "This completed turn is safe to reuse.",
        }
    ]
    transcript = conversation_transcript(
        db_session,
        conversation,
        scope=scope,
    )
    assert transcript[:2] == [
        {
            "role": "user",
            "content": "Attempt the requested analysis.",
            "status": "partial",
            "error_type": "ProviderTimeout",
        },
        {
            "role": "assistant",
            "content": "The request stopped after one confirmed mutation.",
            "status": "partial",
            "error_type": "ProviderTimeout",
        },
    ]
