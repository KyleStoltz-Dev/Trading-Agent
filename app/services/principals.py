"""Hosted API principal authentication and exact account authorization."""

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ApiPrincipal,
    ApiPrincipalGrant,
    SecurityAuditEvent,
)
from app.services.workspaces import RequestScope, validate_scope


@dataclass(frozen=True)
class AuthorizedPrincipal:
    id: uuid.UUID
    subject: str
    role: str
    scope: RequestScope


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_principal(db: Session, subject: str) -> tuple[ApiPrincipal, str]:
    normalized = subject.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._:/-]{2,159}", normalized):
        raise ValueError("principal subject is invalid")
    if db.scalar(select(ApiPrincipal).where(ApiPrincipal.subject == normalized)):
        raise ValueError("principal subject already exists")
    token = secrets.token_urlsafe(32)
    principal = ApiPrincipal(
        subject=normalized,
        token_sha256=_token_digest(token),
    )
    db.add(principal)
    db.commit()
    db.refresh(principal)
    return principal, token


def rotate_principal_token(
    db: Session,
    principal_id: uuid.UUID,
    *,
    scope: RequestScope,
    actor: str,
) -> str:
    validate_scope(db, scope)
    principal = db.get(ApiPrincipal, principal_id)
    if principal is None or not principal.active:
        raise LookupError("active principal was not found")
    grant = db.scalar(
        select(ApiPrincipalGrant).where(
            ApiPrincipalGrant.principal_id == principal.id,
            ApiPrincipalGrant.workspace_id == scope.workspace_id,
            ApiPrincipalGrant.account_id == scope.account_id,
            ApiPrincipalGrant.active.is_(True),
        )
    )
    if grant is None:
        raise LookupError("principal is not granted to the selected account")
    token = secrets.token_urlsafe(32)
    principal.token_sha256 = _token_digest(token)
    grants = list(
        db.scalars(
            select(ApiPrincipalGrant).where(
                ApiPrincipalGrant.principal_id == principal.id,
                ApiPrincipalGrant.active.is_(True),
            )
        )
    )
    for affected in grants:
        db.add(
            SecurityAuditEvent(
                workspace_id=affected.workspace_id,
                account_id=affected.account_id,
                action="principal_token_rotated",
                actor=actor,
                metadata_json={"principal_id": str(principal.id)},
            )
        )
    db.commit()
    return token


def revoke_principal_grant(
    db: Session,
    *,
    principal_id: uuid.UUID,
    scope: RequestScope,
    actor: str,
) -> None:
    validate_scope(db, scope)
    grant = db.scalar(
        select(ApiPrincipalGrant).where(
            ApiPrincipalGrant.principal_id == principal_id,
            ApiPrincipalGrant.workspace_id == scope.workspace_id,
            ApiPrincipalGrant.account_id == scope.account_id,
            ApiPrincipalGrant.active.is_(True),
        )
    )
    if grant is None:
        raise LookupError("active principal grant was not found")
    grant.active = False
    db.add(
        SecurityAuditEvent(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            action="principal_revoked",
            actor=actor,
            metadata_json={"principal_id": str(principal_id)},
        )
    )
    db.commit()


def grant_principal(
    db: Session,
    *,
    principal_id: uuid.UUID,
    scope: RequestScope,
    role: str,
    actor: str,
) -> ApiPrincipalGrant:
    if role not in {"reader", "trader", "admin"}:
        raise ValueError("principal role must be reader, trader, or admin")
    validate_scope(db, scope)
    principal = db.get(ApiPrincipal, principal_id)
    if principal is None or not principal.active:
        raise LookupError("active principal was not found")
    grant = db.scalar(
        select(ApiPrincipalGrant).where(
            ApiPrincipalGrant.principal_id == principal.id,
            ApiPrincipalGrant.workspace_id == scope.workspace_id,
            ApiPrincipalGrant.account_id == scope.account_id,
        )
    )
    if grant is None:
        grant = ApiPrincipalGrant(
            principal_id=principal.id,
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
        )
        db.add(grant)
    grant.role = role
    grant.active = True
    db.add(
        SecurityAuditEvent(
            workspace_id=scope.workspace_id,
            account_id=scope.account_id,
            action="principal_granted",
            actor=actor,
            metadata_json={"principal_id": str(principal.id), "role": role},
        )
    )
    db.commit()
    db.refresh(grant)
    return grant


def authenticate_principal(
    db: Session,
    *,
    bearer_token: str,
    scope: RequestScope,
) -> AuthorizedPrincipal | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", bearer_token):
        return None
    principal = db.scalar(
        select(ApiPrincipal).where(
            ApiPrincipal.token_sha256 == _token_digest(bearer_token),
            ApiPrincipal.active.is_(True),
        )
    )
    if principal is None:
        return None
    grant = db.scalar(
        select(ApiPrincipalGrant).where(
            ApiPrincipalGrant.principal_id == principal.id,
            ApiPrincipalGrant.workspace_id == scope.workspace_id,
            ApiPrincipalGrant.account_id == scope.account_id,
            ApiPrincipalGrant.active.is_(True),
        )
    )
    if grant is None:
        return None
    return AuthorizedPrincipal(
        id=principal.id,
        subject=principal.subject,
        role=grant.role,
        scope=scope,
    )
