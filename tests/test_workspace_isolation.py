import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    AccountConstraintProfile,
    ConversationSession,
    ConversationTurn,
    Instrument,
    MindsetCheckIn,
    Playbook,
    PlaybookVersion,
    PretradeAssessment,
    Trade,
    TradePlan,
    TraderProfile,
    TradingAccount,
    Workspace,
)
from app.services.catalog import configure_account
from app.services.workspaces import (
    BOOTSTRAP_ACCOUNT_LABEL,
    RequestScope,
    bootstrap_initial_scope,
    resolve_current_scope,
    validate_scope,
)


def _workspace(db_session, *, slug: str) -> Workspace:
    workspace = Workspace(slug=slug, name=slug.replace("-", " ").title())
    db_session.add(workspace)
    db_session.flush()
    return workspace


def test_empty_database_bootstraps_one_usable_manual_account(db_session) -> None:
    workspace = _workspace(db_session, slug=f"empty-{uuid.uuid4().hex}")
    archived = TradingAccount(
        workspace_id=workspace.id,
        broker="manual",
        external_account_id="legacy-unassigned",
        label="Legacy / unassigned",
        currency="USD",
        mode="practice",
        active=False,
        is_default=False,
    )
    db_session.add(archived)
    db_session.flush()

    scope, bootstrapped_workspace, account = bootstrap_initial_scope(
        db_session,
        workspace_reference=workspace.slug,
    )

    assert bootstrapped_workspace.id == workspace.id
    assert account.label == BOOTSTRAP_ACCOUNT_LABEL
    assert account.active is True
    assert account.is_default is True
    assert resolve_current_scope(
        db_session,
        workspace_reference=workspace.slug,
        account_reference=str(account.id),
    ) == scope


def test_bootstrap_refuses_to_guess_when_an_active_workspace_exists(
    db_session,
) -> None:
    _workspace(db_session, slug=f"existing-{uuid.uuid4().hex}")

    with pytest.raises(LookupError, match="configured workspace was not found"):
        bootstrap_initial_scope(
            db_session,
            workspace_reference=f"misspelled-{uuid.uuid4().hex}",
        )


def test_reconfiguring_account_refreshes_mutable_metadata(db_session) -> None:
    workspace = _workspace(
        db_session,
        slug=f"configure-{uuid.uuid4().hex}",
    )
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="OANDA",
        external_account_id="001",
        label="Old label",
        currency="USD",
        mode="practice",
        provider="oanda-v20",
        environment="practice",
        config_reference="env:OLD",
    )
    account.active = False
    connection.status = "disabled"
    db_session.commit()

    refreshed, refreshed_connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="OANDA",
        external_account_id="001",
        label="New label",
        currency="EUR",
        mode="live",
        provider="oanda-v20",
        environment="live",
        config_reference="env:OANDA_API_TOKEN",
    )

    assert refreshed.id == account.id
    assert refreshed.label == "New label"
    assert refreshed.currency == "EUR"
    assert refreshed.mode == "live"
    assert refreshed.active is True
    assert refreshed_connection.id == connection.id
    assert refreshed_connection.environment == "live"
    assert refreshed_connection.config_reference == "env:OANDA_API_TOKEN"
    assert refreshed_connection.status == "configured"


@pytest.mark.parametrize("archive_workspace", [False, True])
def test_explicit_scope_rejects_inactive_identity(
    db_session,
    archive_workspace,
) -> None:
    workspace = _workspace(
        db_session,
        slug=f"inactive-scope-{uuid.uuid4().hex}",
    )
    account = _account(
        db_session,
        workspace,
        external_id=f"inactive-{uuid.uuid4().hex}",
        label="Inactive scope",
    )
    if archive_workspace:
        workspace.active = False
    else:
        account.active = False
    db_session.commit()

    with pytest.raises(LookupError, match="account was not found"):
        validate_scope(
            db_session,
            RequestScope(
                workspace_id=workspace.id,
                account_id=account.id,
            ),
        )


def _account(
    db_session,
    workspace: Workspace,
    *,
    external_id: str,
    label: str,
) -> TradingAccount:
    account = TradingAccount(
        workspace_id=workspace.id,
        broker="test-broker",
        external_account_id=external_id,
        label=label,
        currency="USD",
        mode="practice",
        is_default=False,
    )
    db_session.add(account)
    db_session.flush()
    return account


def _profile(
    db_session,
    workspace: Workspace,
    account: TradingAccount,
    *,
    profile_key: str = "local",
) -> TraderProfile:
    profile = TraderProfile(
        workspace_id=workspace.id,
        account_id=account.id,
        profile_key=profile_key,
        display_name="Test Trader",
        timezone="America/New_York",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _version(
    db_session,
    workspace: Workspace,
    *,
    name: str = "Wyckoff",
) -> PlaybookVersion:
    playbook = Playbook(
        workspace_id=workspace.id,
        name=name,
        description="Isolation-test playbook.",
    )
    db_session.add(playbook)
    db_session.flush()
    version = PlaybookVersion(
        workspace_id=workspace.id,
        playbook_id=playbook.id,
        version=1,
        definition={"methodology": "wyckoff", "setups": []},
        content_hash=uuid.uuid4().hex.ljust(64, "0"),
    )
    db_session.add(version)
    db_session.flush()
    return version


def _instrument(db_session) -> Instrument:
    instrument = Instrument(
        canonical_symbol=f"XAUUSD-{uuid.uuid4().hex}",
        display_name="Gold",
        asset_class="commodity",
        base_currency="XAU",
        quote_currency="USD",
        price_precision=2,
        quantity_precision=2,
    )
    db_session.add(instrument)
    db_session.flush()
    return instrument


def _constraint(
    db_session,
    workspace: Workspace,
    account: TradingAccount,
    profile: TraderProfile,
    *,
    name: str = "Primary rules",
) -> AccountConstraintProfile:
    constraint = AccountConstraintProfile(
        workspace_id=workspace.id,
        trading_account_id=account.id,
        profile_id=profile.id,
        name=name,
        account_type="personal",
        account_size=Decimal("25000"),
        currency="USD",
        phase="personal",
        rule_limits={},
        active=True,
    )
    db_session.add(constraint)
    db_session.flush()
    return constraint


def _plan(
    db_session,
    workspace: Workspace,
    account: TradingAccount,
    version: PlaybookVersion,
    *,
    reference: str,
    trade_id: uuid.UUID | None = None,
    flush: bool = True,
) -> TradePlan:
    plan = TradePlan(
        workspace_id=workspace.id,
        reference=reference,
        trade_id=trade_id,
        account_id=account.id,
        playbook_version_id=version.id,
        instrument="XAUUSD",
        direction="long",
        setup_name="spring",
        context_timeframe="H1",
        trigger_timeframe="M5",
        entry=Decimal("2300"),
        stop=Decimal("2295"),
        target=Decimal("2315"),
        account_equity=Decimal("25000"),
        risk_percent=Decimal("0.5"),
        value_per_price_unit=Decimal("1"),
        risk_amount=Decimal("125"),
        quantity=Decimal("25"),
        planned_r=Decimal("3"),
        thesis="Price reclaimed the defined range.",
        invalidation="Price accepts below the spring low.",
        observations=["Price reclaimed the range."],
        interpretations=["The spring hypothesis remains possible."],
        status="planned",
    )
    db_session.add(plan)
    if flush:
        db_session.flush()
    return plan


def _mindset(
    db_session,
    workspace: Workspace,
    account: TradingAccount,
    version: PlaybookVersion,
    *,
    plan: TradePlan | None = None,
) -> MindsetCheckIn:
    mindset = MindsetCheckIn(
        workspace_id=workspace.id,
        account_id=account.id,
        playbook_version_id=version.id,
        trade_plan_id=None if plan is None else plan.id,
        phase="pre_trade",
        readiness=4,
        accepted_risk=True,
        emotion_tags=["focused"],
    )
    db_session.add(mindset)
    db_session.flush()
    return mindset


def _assessment(
    workspace: Workspace,
    account: TradingAccount,
    version: PlaybookVersion,
    *,
    mindset_id: uuid.UUID | None = None,
    constraint_id: uuid.UUID | None = None,
    trade_plan_id: uuid.UUID | None = None,
) -> PretradeAssessment:
    return PretradeAssessment(
        workspace_id=workspace.id,
        account_id=account.id,
        playbook_version_id=version.id,
        mindset_checkin_id=mindset_id,
        account_constraint_profile_id=constraint_id,
        trade_plan_id=trade_plan_id,
        setup_key="spring",
        rating="conditional",
        component_scores={},
        hard_blockers=[],
        stand_aside_reasons=[],
        missing_evidence=[],
        rule_results=[],
        news_status="not_configured",
        market_context={},
        policy_hash="a" * 64,
        human_decision="pending",
    )


def test_decision_and_memory_scope_columns_are_required() -> None:
    required_columns = (
        TradingAccount.workspace_id,
        Trade.workspace_id,
        Trade.account_id,
        TradePlan.workspace_id,
        TradePlan.account_id,
        MindsetCheckIn.workspace_id,
        MindsetCheckIn.account_id,
        PretradeAssessment.workspace_id,
        PretradeAssessment.account_id,
        ConversationSession.workspace_id,
        ConversationSession.account_id,
        ConversationTurn.workspace_id,
        ConversationTurn.account_id,
        AccountConstraintProfile.workspace_id,
        AccountConstraintProfile.trading_account_id,
        AccountConstraintProfile.profile_id,
    )

    assert all(column.property.columns[0].nullable is False for column in required_columns)


def test_trade_plan_rejects_trade_from_another_account_in_same_workspace(
    db_session,
) -> None:
    workspace = _workspace(db_session, slug=f"plan-scope-{uuid.uuid4().hex}")
    account_a = _account(
        db_session,
        workspace,
        external_id=f"a-{uuid.uuid4().hex}",
        label="Account A",
    )
    account_b = _account(
        db_session,
        workspace,
        external_id=f"b-{uuid.uuid4().hex}",
        label="Account B",
    )
    version = _version(db_session, workspace)
    instrument = _instrument(db_session)
    trade_a = Trade(
        workspace_id=workspace.id,
        account_id=account_a.id,
        instrument_id=instrument.id,
        external_trade_id=f"trade-{uuid.uuid4().hex}",
        direction="long",
        status="open",
    )
    db_session.add(trade_a)
    db_session.flush()

    _plan(
        db_session,
        workspace,
        account_b,
        version,
        reference=f"cross-account-{uuid.uuid4().hex}",
        trade_id=trade_a.id,
        flush=False,
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_conversation_session_rejects_account_from_another_workspace(
    db_session,
) -> None:
    workspace_a = _workspace(db_session, slug=f"workspace-a-{uuid.uuid4().hex}")
    workspace_b = _workspace(db_session, slug=f"workspace-b-{uuid.uuid4().hex}")
    account_b = _account(
        db_session,
        workspace_b,
        external_id=f"b-{uuid.uuid4().hex}",
        label="Workspace B",
    )

    db_session.add(
        ConversationSession(
            workspace_id=workspace_a.id,
            account_id=account_b.id,
            name="cross-workspace",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_conversation_turn_rejects_session_from_another_account_in_same_workspace(
    db_session,
) -> None:
    workspace = _workspace(db_session, slug=f"turn-scope-{uuid.uuid4().hex}")
    account_a = _account(
        db_session,
        workspace,
        external_id=f"a-{uuid.uuid4().hex}",
        label="Account A",
    )
    account_b = _account(
        db_session,
        workspace,
        external_id=f"b-{uuid.uuid4().hex}",
        label="Account B",
    )
    session_a = ConversationSession(
        workspace_id=workspace.id,
        account_id=account_a.id,
        name="account-a-session",
    )
    db_session.add(session_a)
    db_session.flush()

    db_session.add(
        ConversationTurn(
            workspace_id=workspace.id,
            account_id=account_b.id,
            session_id=session_a.id,
            role="user",
            content="This turn must not enter account A's memory.",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_mindset_rejects_plan_from_another_account_in_same_workspace(
    db_session,
) -> None:
    workspace = _workspace(db_session, slug=f"mindset-scope-{uuid.uuid4().hex}")
    account_a = _account(
        db_session,
        workspace,
        external_id=f"a-{uuid.uuid4().hex}",
        label="Account A",
    )
    account_b = _account(
        db_session,
        workspace,
        external_id=f"b-{uuid.uuid4().hex}",
        label="Account B",
    )
    version = _version(db_session, workspace)
    plan_a = _plan(
        db_session,
        workspace,
        account_a,
        version,
        reference=f"plan-a-{uuid.uuid4().hex}",
    )

    db_session.add(
        MindsetCheckIn(
            workspace_id=workspace.id,
            account_id=account_b.id,
            playbook_version_id=version.id,
            trade_plan_id=plan_a.id,
            phase="pre_trade",
            readiness=4,
            accepted_risk=True,
            emotion_tags=["focused"],
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("mismatched_link", ("mindset", "constraint", "trade_plan"))
def test_pretrade_rejects_links_from_another_account_in_same_workspace(
    db_session,
    mismatched_link: str,
) -> None:
    workspace = _workspace(db_session, slug=f"pretrade-scope-{uuid.uuid4().hex}")
    account_a = _account(
        db_session,
        workspace,
        external_id=f"a-{uuid.uuid4().hex}",
        label="Account A",
    )
    account_b = _account(
        db_session,
        workspace,
        external_id=f"b-{uuid.uuid4().hex}",
        label="Account B",
    )
    profile = _profile(db_session, workspace, account_b)
    version = _version(db_session, workspace)
    plan_b = _plan(
        db_session,
        workspace,
        account_b,
        version,
        reference=f"plan-b-{uuid.uuid4().hex}",
    )
    mindset_b = _mindset(db_session, workspace, account_b, version)
    constraint_b = _constraint(db_session, workspace, account_b, profile)

    link_values = {
        "mindset_id": mindset_b.id if mismatched_link == "mindset" else None,
        "constraint_id": constraint_b.id if mismatched_link == "constraint" else None,
        "trade_plan_id": plan_b.id if mismatched_link == "trade_plan" else None,
    }
    db_session.add(
        _assessment(
            workspace,
            account_a,
            version,
            **link_values,
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("mismatched_parent", ("account", "profile"))
def test_account_constraint_rejects_parent_from_another_workspace(
    db_session,
    mismatched_parent: str,
) -> None:
    workspace_a = _workspace(db_session, slug=f"constraint-a-{uuid.uuid4().hex}")
    workspace_b = _workspace(db_session, slug=f"constraint-b-{uuid.uuid4().hex}")
    account_a = _account(
        db_session,
        workspace_a,
        external_id=f"a-{uuid.uuid4().hex}",
        label="Account A",
    )
    account_b = _account(
        db_session,
        workspace_b,
        external_id=f"b-{uuid.uuid4().hex}",
        label="Account B",
    )
    profile_a = _profile(db_session, workspace_a, account_a)
    profile_b = _profile(db_session, workspace_b, account_b)

    db_session.add(
        AccountConstraintProfile(
            workspace_id=workspace_a.id,
            trading_account_id=(
                account_b.id if mismatched_parent == "account" else account_a.id
            ),
            profile_id=(
                profile_b.id if mismatched_parent == "profile" else profile_a.id
            ),
            name="Cross-workspace rules",
            account_type="personal",
            account_size=Decimal("25000"),
            currency="USD",
            phase="personal",
            rule_limits={},
            active=True,
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_facing_names_can_repeat_in_independent_scopes(db_session) -> None:
    workspace_a = _workspace(db_session, slug=f"names-a-{uuid.uuid4().hex}")
    workspace_b = _workspace(db_session, slug=f"names-b-{uuid.uuid4().hex}")
    account_a1 = _account(
        db_session,
        workspace_a,
        external_id="shared-external-id",
        label="Primary",
    )
    account_a2 = _account(
        db_session,
        workspace_a,
        external_id=f"second-{uuid.uuid4().hex}",
        label="Secondary",
    )
    account_b = _account(
        db_session,
        workspace_b,
        external_id="shared-external-id",
        label="Primary",
    )
    profile_a = _profile(
        db_session,
        workspace_a,
        account_a1,
        profile_key="shared-profile",
    )
    profile_a2 = _profile(
        db_session,
        workspace_a,
        account_a2,
        profile_key="shared-profile",
    )
    _profile(
        db_session,
        workspace_b,
        account_b,
        profile_key="shared-profile",
    )
    version_a = _version(db_session, workspace_a, name="Wyckoff")
    _version(db_session, workspace_b, name="Wyckoff")

    db_session.add_all(
        [
            ConversationSession(
                workspace_id=workspace_a.id,
                account_id=account_a1.id,
                name="morning",
            ),
            ConversationSession(
                workspace_id=workspace_a.id,
                account_id=account_a2.id,
                name="morning",
            ),
        ]
    )
    _constraint(
        db_session,
        workspace_a,
        account_a1,
        profile_a,
        name="Primary rules",
    )
    _constraint(
        db_session,
        workspace_a,
        account_a2,
        profile_a2,
        name="Primary rules",
    )
    _plan(
        db_session,
        workspace_a,
        account_a1,
        version_a,
        reference="morning-plan",
    )
    _plan(
        db_session,
        workspace_a,
        account_a2,
        version_a,
        reference="morning-plan",
    )

    db_session.flush()
    assert account_b.workspace_id == workspace_b.id
