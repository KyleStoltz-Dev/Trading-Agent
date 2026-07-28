import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import LEGACY_UNASSIGNED_ACCOUNT_ID, LEGACY_WORKSPACE_ID
from app.models import PlaybookVersion, TradingAccount, Workspace

BOOTSTRAP_ACCOUNT_EXTERNAL_ID = "local-journal"
BOOTSTRAP_ACCOUNT_LABEL = "Manual / journal"
WORKSPACE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")


@dataclass(frozen=True, slots=True)
class RequestScope:
    """The mandatory tenant and trading-account boundary for one operation."""

    workspace_id: uuid.UUID
    account_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, uuid.UUID):
            raise TypeError("workspace_id must be a UUID")
        if not isinstance(self.account_id, uuid.UUID):
            raise TypeError("account_id must be a UUID")


def list_workspaces(
    db: Session,
    *,
    active_only: bool = True,
) -> list[Workspace]:
    statement = select(Workspace)
    if active_only:
        statement = statement.where(Workspace.active.is_(True))
    return list(db.scalars(statement.order_by(Workspace.name, Workspace.slug)))


def resolve_workspace(
    db: Session,
    reference: str | uuid.UUID,
    *,
    active_only: bool = True,
) -> Workspace | None:
    """Resolve a workspace by exact UUID or case-insensitive slug."""
    if isinstance(reference, uuid.UUID):
        identity = reference
        slug = None
    else:
        value = reference.strip()
        try:
            identity = uuid.UUID(value)
            slug = None
        except ValueError:
            identity = None
            slug = value.lower()

    statement = select(Workspace)
    if identity is not None:
        statement = statement.where(Workspace.id == identity)
    elif slug:
        statement = statement.where(func.lower(Workspace.slug) == slug)
    else:
        return None
    if active_only:
        statement = statement.where(Workspace.active.is_(True))
    return db.scalar(statement)


def list_accounts(
    db: Session,
    workspace_id: uuid.UUID,
    *,
    active_only: bool = True,
) -> list[TradingAccount]:
    """List accounts from exactly one explicit workspace."""
    _require_workspace(db, workspace_id, active_only=active_only)
    statement = select(TradingAccount).where(
        TradingAccount.workspace_id == workspace_id
    )
    if active_only:
        statement = statement.where(TradingAccount.active.is_(True))
    return list(
        db.scalars(
            statement.order_by(
                TradingAccount.is_default.desc(),
                TradingAccount.label,
                TradingAccount.id,
            )
        )
    )


def resolve_account(
    db: Session,
    workspace_id: uuid.UUID,
    reference: str | uuid.UUID,
    *,
    active_only: bool = True,
) -> TradingAccount | None:
    """Resolve an account without ever searching outside the given workspace."""
    _require_workspace(db, workspace_id, active_only=active_only)
    statement = select(TradingAccount).where(
        TradingAccount.workspace_id == workspace_id
    )
    if isinstance(reference, uuid.UUID):
        statement = statement.where(TradingAccount.id == reference)
    else:
        value = reference.strip()
        try:
            identity = uuid.UUID(value)
        except ValueError:
            identity = None
        if identity is not None:
            statement = statement.where(TradingAccount.id == identity)
        elif value:
            normalized = value.lower()
            statement = statement.where(
                or_(
                    func.lower(TradingAccount.label) == normalized,
                    func.lower(TradingAccount.external_account_id) == normalized,
                )
            )
        else:
            return None
    if active_only:
        statement = statement.where(TradingAccount.active.is_(True))

    matches = list(db.scalars(statement.limit(2)))
    if len(matches) > 1:
        raise ValueError("account reference is ambiguous within the workspace")
    return matches[0] if matches else None


def resolve_scope(
    db: Session,
    *,
    workspace_reference: str | uuid.UUID,
    account_reference: str | uuid.UUID,
) -> RequestScope:
    """Resolve both explicit references into a validated immutable scope."""
    workspace = resolve_workspace(db, workspace_reference)
    if workspace is None:
        raise LookupError("workspace was not found")
    account = resolve_account(db, workspace.id, account_reference)
    if account is None:
        raise LookupError("account was not found in the requested workspace")
    return RequestScope(workspace_id=workspace.id, account_id=account.id)


def resolve_current_scope(
    db: Session,
    *,
    workspace_reference: str | uuid.UUID,
    account_reference: str | uuid.UUID | None = None,
) -> RequestScope:
    """Resolve the CLI/API edge selection; ambiguity is never guessed."""
    workspace = resolve_workspace(db, workspace_reference)
    if workspace is None:
        raise LookupError("configured workspace was not found")
    if account_reference is not None:
        account = resolve_account(db, workspace.id, account_reference)
        if account is None:
            raise LookupError("configured account was not found in the workspace")
        return RequestScope(workspace_id=workspace.id, account_id=account.id)

    defaults = list(
        db.scalars(
            select(TradingAccount).where(
                TradingAccount.workspace_id == workspace.id,
                TradingAccount.active.is_(True),
                TradingAccount.is_default.is_(True),
            )
        )
    )
    if len(defaults) == 1:
        return RequestScope(workspace_id=workspace.id, account_id=defaults[0].id)
    accounts = list_accounts(db, workspace.id)
    if len(accounts) == 1:
        return RequestScope(workspace_id=workspace.id, account_id=accounts[0].id)
    if not accounts:
        raise LookupError("workspace has no active trading account")
    raise LookupError(
        "workspace has multiple accounts and no default; select an account explicitly"
    )


def bootstrap_initial_scope(
    db: Session,
    *,
    workspace_reference: str,
) -> tuple[RequestScope, Workspace, TradingAccount]:
    """Create the first usable local scope, but never guess in a populated database.

    The post-migration ``Legacy / unassigned`` account is intentionally inactive.
    A brand-new installation therefore needs one active journal account before
    onboarding can store account-scoped profile data.
    """
    requested = workspace_reference.strip().lower()
    workspace = resolve_workspace(db, requested, active_only=False)
    active_workspaces = list_workspaces(db, active_only=True)
    if workspace is None:
        if active_workspaces:
            raise LookupError(
                "configured workspace was not found; run `trade account list` "
                "or correct TRADING_WORKSPACE"
            )
        if not requested:
            requested = "trading"
        if not WORKSPACE_SLUG.fullmatch(requested):
            raise ValueError(
                "TRADING_WORKSPACE must be 1–80 lowercase letters, numbers, or hyphens"
            )
        workspace = Workspace(
            slug=requested,
            name="Trading workspace",
            active=True,
        )
        db.add(workspace)
        db.flush()
    elif not workspace.active:
        raise LookupError(
            "configured workspace is archived; choose an active workspace"
        )

    active_accounts = list_accounts(db, workspace.id)
    if active_accounts:
        raise LookupError(
            "workspace already has an active account; select it explicitly instead "
            "of creating a bootstrap account"
        )
    non_bootstrap_accounts = list(
        db.scalars(
            select(TradingAccount).where(
                TradingAccount.workspace_id == workspace.id,
                TradingAccount.external_account_id != "legacy-unassigned",
            )
        )
    )
    if non_bootstrap_accounts:
        raise LookupError(
            "workspace has archived accounts; recover or explicitly select one "
            "before creating new account data"
        )
    account = TradingAccount(
        workspace_id=workspace.id,
        broker="manual",
        external_account_id=BOOTSTRAP_ACCOUNT_EXTERNAL_ID,
        label=BOOTSTRAP_ACCOUNT_LABEL,
        currency="USD",
        mode="practice",
        active=True,
        is_default=True,
    )
    db.add(account)
    db.flush()
    return (
        RequestScope(workspace_id=workspace.id, account_id=account.id),
        workspace,
        account,
    )


def validate_scope(db: Session, scope: RequestScope) -> TradingAccount:
    """Fail closed unless both scope identities exist and belong together."""
    if not isinstance(scope, RequestScope):
        raise TypeError("scope must be an explicit RequestScope")
    legacy_scope = (
        scope.workspace_id == uuid.UUID(LEGACY_WORKSPACE_ID)
        and scope.account_id == uuid.UUID(LEGACY_UNASSIGNED_ACCOUNT_ID)
    )
    account = db.scalar(
        select(TradingAccount)
        .join(Workspace, Workspace.id == TradingAccount.workspace_id)
        .where(
            Workspace.active.is_(True),
            TradingAccount.workspace_id == scope.workspace_id,
            TradingAccount.id == scope.account_id,
            (
                TradingAccount.id == scope.account_id
                if legacy_scope
                else TradingAccount.active.is_(True)
            ),
        )
    )
    if account is None:
        raise LookupError("account was not found in the requested workspace")
    return account


def validate_strategy_scope(
    db: Session,
    scope: RequestScope,
    playbook_version_id: uuid.UUID | None,
) -> PlaybookVersion | None:
    """Validate an optional immutable strategy version against the workspace."""
    validate_scope(db, scope)
    if playbook_version_id is None:
        return None
    version = db.scalar(
        select(PlaybookVersion).where(
            PlaybookVersion.workspace_id == scope.workspace_id,
            PlaybookVersion.id == playbook_version_id,
        )
    )
    if version is None:
        raise LookupError("strategy version was not found in the requested workspace")
    return version


def _require_workspace(
    db: Session,
    workspace_id: uuid.UUID,
    *,
    active_only: bool = True,
) -> Workspace:
    statement = select(Workspace).where(Workspace.id == workspace_id)
    if active_only:
        statement = statement.where(Workspace.active.is_(True))
    workspace = db.scalar(statement)
    if workspace is None:
        raise LookupError("workspace was not found")
    return workspace
