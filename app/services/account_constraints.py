"""Trader-defined account and prop-program constraints."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import AccountConstraintProfile, TraderProfile
from app.schemas import (
    AccountConstraintRead,
    AccountConstraintUpsert,
    AccountRuleLimits,
)
from app.services.workspaces import RequestScope, validate_scope


def account_constraint_read(
    account: AccountConstraintProfile,
) -> AccountConstraintRead:
    return AccountConstraintRead(
        id=account.id,
        workspace_id=account.workspace_id,
        trading_account_id=account.trading_account_id,
        profile_id=account.profile_id,
        name=account.name,
        account_type=account.account_type,
        account_size=Decimal(account.account_size),
        currency=account.currency,
        firm_name=account.firm_name,
        program_name=account.program_name,
        phase=account.phase,
        rules=AccountRuleLimits.model_validate(account.rule_limits),
        active=account.active,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def active_account_constraint(
    db: Session,
    profile_id: uuid.UUID,
    *,
    scope: RequestScope,
) -> AccountConstraintRead | None:
    validate_scope(db, scope)
    profile = db.scalar(
        select(TraderProfile).where(
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.id == profile_id,
        )
    )
    if profile is None:
        raise LookupError("trader profile was not found in the requested workspace")
    account = db.scalar(
        select(AccountConstraintProfile)
        .where(
            AccountConstraintProfile.profile_id == profile_id,
            AccountConstraintProfile.workspace_id == scope.workspace_id,
            AccountConstraintProfile.trading_account_id == scope.account_id,
            AccountConstraintProfile.active.is_(True),
        )
        .order_by(AccountConstraintProfile.updated_at.desc())
        .limit(1)
    )
    return account_constraint_read(account) if account is not None else None


def upsert_active_account_constraint(
    db: Session,
    profile: TraderProfile,
    request: AccountConstraintUpsert,
    *,
    scope: RequestScope,
    commit: bool = True,
) -> AccountConstraintRead:
    validate_scope(db, scope)
    if profile.workspace_id != scope.workspace_id:
        raise LookupError("trader profile was not found in the requested workspace")
    account = db.scalar(
        select(AccountConstraintProfile).where(
            AccountConstraintProfile.workspace_id == scope.workspace_id,
            AccountConstraintProfile.trading_account_id == scope.account_id,
            AccountConstraintProfile.profile_id == profile.id,
            AccountConstraintProfile.name == request.name,
        )
    )
    db.execute(
        update(AccountConstraintProfile)
        .where(
            AccountConstraintProfile.workspace_id == scope.workspace_id,
            AccountConstraintProfile.trading_account_id == scope.account_id,
            AccountConstraintProfile.profile_id == profile.id,
            AccountConstraintProfile.active.is_(True),
        )
        .values(active=False)
    )
    values = request.model_dump(mode="json")
    rules = values.pop("rules")
    if account is None:
        account = AccountConstraintProfile(
            workspace_id=scope.workspace_id,
            trading_account_id=scope.account_id,
            profile_id=profile.id,
            **values,
            rule_limits=rules,
            active=True,
        )
        db.add(account)
    else:
        for key, value in values.items():
            setattr(account, key, value)
        account.rule_limits = rules
        account.active = True
    if commit:
        db.commit()
        db.refresh(account)
    else:
        db.flush()
    return account_constraint_read(account)


def deactivate_account_constraints(
    db: Session,
    profile_id: uuid.UUID,
    *,
    scope: RequestScope,
    commit: bool = True,
) -> None:
    validate_scope(db, scope)
    profile = db.scalar(
        select(TraderProfile).where(
            TraderProfile.workspace_id == scope.workspace_id,
            TraderProfile.id == profile_id,
        )
    )
    if profile is None:
        raise LookupError("trader profile was not found in the requested workspace")
    db.execute(
        update(AccountConstraintProfile)
        .where(
            AccountConstraintProfile.workspace_id == scope.workspace_id,
            AccountConstraintProfile.trading_account_id == scope.account_id,
            AccountConstraintProfile.profile_id == profile_id,
            AccountConstraintProfile.active.is_(True),
        )
        .values(active=False)
    )
    if commit:
        db.commit()
    else:
        db.flush()


def account_rule_reminders(
    account: AccountConstraintRead | AccountConstraintUpsert,
) -> tuple[str, ...]:
    """Return deterministic, trader-facing reminders without claiming live compliance."""
    rules = account.rules
    size = account.account_size
    reminders: list[str] = []

    def percentage_rule(label: str, value: Decimal | None) -> None:
        if value is None:
            return
        amount = (size * value / Decimal("100")).quantize(Decimal("0.01"))
        reminders.append(
            f"{label}: {format(value.normalize(), 'f')}% "
            f"({account.currency} {amount})"
        )

    percentage_rule("Maximum daily loss", rules.maximum_daily_loss_percent)
    percentage_rule("Maximum total loss", rules.maximum_total_loss_percent)
    percentage_rule("Profit target", rules.profit_target_percent)
    percentage_rule("Consistency limit", rules.consistency_limit_percent)
    if rules.minimum_trading_days is not None:
        reminders.append(f"Minimum trading days: {rules.minimum_trading_days}")
    if rules.maximum_trading_days is not None:
        reminders.append(f"Maximum trading days: {rules.maximum_trading_days}")
    if rules.drawdown_type != "unknown":
        reminders.append(f"Drawdown type: {rules.drawdown_type.replace('_', ' ')}")
    for label, value in (
        ("News trading", rules.news_trading),
        ("Overnight holding", rules.overnight_holding),
        ("Weekend holding", rules.weekend_holding),
    ):
        if value != "unknown":
            reminders.append(f"{label}: {value}")
    if rules.daily_reset_timezone is not None:
        reminders.append(f"Daily reset timezone: {rules.daily_reset_timezone}")
    reminders.extend(f"Custom rule: {rule}" for rule in rules.custom_rules)
    return tuple(reminders)


def unverified_account_rules(
    account: AccountConstraintRead | AccountConstraintUpsert,
) -> tuple[str, ...]:
    rules = account.rules
    missing: list[str] = []
    if rules.maximum_daily_loss_percent is None:
        missing.append("maximum daily loss")
    if rules.maximum_total_loss_percent is None:
        missing.append("maximum total loss")
    if rules.drawdown_type == "unknown":
        missing.append("drawdown calculation")
    if account.account_type == "prop":
        if (
            account.phase in {"evaluation", "verification"}
            and rules.profit_target_percent is None
        ):
            missing.append("profit target")
        if rules.news_trading == "unknown":
            missing.append("news-trading policy")
        if rules.overnight_holding == "unknown":
            missing.append("overnight-holding policy")
        if rules.weekend_holding == "unknown":
            missing.append("weekend-holding policy")
    return tuple(missing)
