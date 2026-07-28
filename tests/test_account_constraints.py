import uuid

import pytest
from sqlalchemy import select

from app.models import AccountConstraintProfile, TraderProfile
from app.schemas import (
    AccountConstraintUpsert,
    AccountRuleLimits,
)
from app.services.account_constraints import (
    account_rule_reminders,
    active_account_constraint,
    unverified_account_rules,
    upsert_active_account_constraint,
)


def _profile(db_session, request_scope) -> TraderProfile:
    profile = TraderProfile(
        workspace_id=request_scope.workspace_id,
        account_id=request_scope.account_id,
        profile_key=f"account-rules-{uuid.uuid4().hex}",
        display_name="Trader",
        timezone="America/New_York",
    )
    db_session.add(profile)
    db_session.commit()
    return profile


def test_prop_account_requires_firm_and_non_personal_phase() -> None:
    with pytest.raises(ValueError, match="prop firm"):
        AccountConstraintUpsert(
            name="Challenge",
            account_type="prop",
            account_size="100000",
            currency="USD",
            phase="evaluation",
        )
    with pytest.raises(ValueError, match="program phase"):
        AccountConstraintUpsert(
            name="Challenge",
            account_type="prop",
            account_size="100000",
            currency="USD",
            firm_name="Example Firm",
            phase="personal",
        )


def test_account_rules_reject_secrets_and_inconsistent_day_ranges() -> None:
    with pytest.raises(ValueError, match="credentials"):
        AccountRuleLimits(
            custom_rules=["OPENAI_API_KEY=sk-abcdefghijklmnop"],
        )
    with pytest.raises(ValueError, match="maximum trading days"):
        AccountRuleLimits(
            minimum_trading_days=10,
            maximum_trading_days=5,
        )


def test_active_account_rules_are_structured_and_amounts_are_deterministic(
    db_session,
    request_scope,
) -> None:
    profile = _profile(db_session, request_scope)
    prop = upsert_active_account_constraint(
        db_session,
        profile,
        AccountConstraintUpsert(
            name="100K evaluation",
            account_type="prop",
            account_size="100000",
            currency="usd",
            firm_name="Example Firm",
            program_name="Phase One",
            phase="evaluation",
            rules=AccountRuleLimits(
                maximum_daily_loss_percent="5",
                maximum_total_loss_percent="10",
                profit_target_percent="8",
                drawdown_type="equity_based",
                news_trading="prohibited",
                overnight_holding="restricted",
                weekend_holding="prohibited",
                daily_reset_timezone="America/New_York",
            ),
        ),
        scope=request_scope,
    )

    assert account_rule_reminders(prop)[:3] == (
        "Maximum daily loss: 5% (USD 5000.00)",
        "Maximum total loss: 10% (USD 10000.00)",
        "Profit target: 8% (USD 8000.00)",
    )
    assert unverified_account_rules(prop) == ()

    personal = upsert_active_account_constraint(
        db_session,
        profile,
        AccountConstraintUpsert(
            name="Personal",
            account_type="personal",
            account_size="25000",
            currency="USD",
            phase="personal",
            rules=AccountRuleLimits(maximum_daily_loss_percent="2"),
        ),
        scope=request_scope,
    )
    active = active_account_constraint(
        db_session,
        profile.id,
        scope=request_scope,
    )
    rows = list(
        db_session.scalars(
            select(AccountConstraintProfile).where(
                AccountConstraintProfile.profile_id == profile.id
            )
        )
    )

    assert active is not None
    assert active.id == personal.id
    assert sum(row.active for row in rows) == 1
    assert unverified_account_rules(personal) == (
        "maximum total loss",
        "drawdown calculation",
    )
