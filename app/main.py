import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.config import get_settings, secret_value
from app.db import (
    SessionLocal,
    bind_database_scope,
    get_db,
    upgrade_database,
    verify_hosted_rls,
)
from app.models import TradePlan, TradeReflection
from app.policy import PolicyEngine, ToolContext
from app.providers import create_model_provider
from app.schemas import (
    ChartAnalysis,
    PositionSizeRequest,
    PositionSizeResult,
    ReflectionCreate,
    ReflectionRead,
    TradePlanCreate,
    TradePlanRead,
    TradingViewAlertRead,
    TradingViewWebhookCreate,
    TradingViewWebhookReceipt,
)
from app.services.chart_analysis import SYSTEM_PROMPT, analyze_chart
from app.services.evidence import record_chart_analysis
from app.services.journal import (
    ReflectionExistsError,
    TradeNotFoundError,
    create_reflection,
    create_trade_plan,
    get_trade_plan,
    list_trade_plans,
)
from app.services.principals import authenticate_principal
from app.services.risk import calculate_position_size
from app.services.secrets import validate_secret_backend
from app.services.tool_audit import (
    complete_mutation_audit,
    record_direct_cli_confirmation,
)
from app.services.tradingview import (
    TradingViewEventConflictError,
    ingest_tradingview_alert,
    recent_tradingview_alerts,
    tradingview_webhook_secret_is_valid,
    trusted_proxy_networks,
)
from app.services.workspaces import (
    RequestScope,
    resolve_account,
    resolve_workspace,
    validate_scope,
    validate_strategy_scope,
)


class WebhookRateLimiter:
    """Bound recent requests per security scope and source."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(
        self,
        scope_key: str | uuid.UUID,
        source_ip: str,
        *,
        limit: int,
    ) -> bool:
        now = self._clock()
        cutoff = now - 60
        key = (str(scope_key), source_ip)
        with self._lock:
            recent = self._events[key]
            while recent and recent[0] <= cutoff:
                recent.popleft()
            if len(recent) >= limit:
                return False
            recent.append(now)
            return True


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.policy = PolicyEngine.load()
    application.state.confirmations = ConfirmationStore(
        ttl_seconds=get_settings().api_confirmation_ttl_seconds
    )
    application.state.tradingview_rate_limiter = WebhookRateLimiter()
    application.state.api_rate_limiter = WebhookRateLimiter()
    if get_settings().database_auto_migrate:
        upgrade_database()
    if get_settings().deployment_mode == "hosted-multi-user":
        validate_secret_backend(get_settings())
        verify_hosted_rls()
    yield


app = FastAPI(
    title="Trading Agent",
    version="0.1.0",
    description="Human-in-the-loop trading playbook and journal.",
    lifespan=lifespan,
)

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TRADINGVIEW_WEBHOOK_PATH = "/api/webhooks/tradingview/{account_id}"
TRADINGVIEW_WEBHOOK_PREFIX = "/api/webhooks/tradingview/"
TRADINGVIEW_SOURCE_IPS = frozenset(
    {
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    }
)
TRADINGVIEW_CERTIFICATE_IDENTITY = "webhook-server@tradingview.com"


def _api_key_is_valid(api_key: str | None) -> bool:
    expected = secret_value(get_settings().trading_agent_api_key)
    return bool(
        expected is not None
        and len(expected) >= 32
        and api_key is not None
        and hmac.compare_digest(expected, api_key)
    )


@app.middleware("http")
async def bind_confirmation_to_raw_body(
    request: Request,
    call_next,
):
    """Hash raw API mutation bytes before JSON or multipart parsing consumes them."""
    if request.method in MUTATING_METHODS and request.url.path.startswith("/api/"):
        tradingview_delivery = (
            request.method == "POST"
            and request.url.path.startswith(TRADINGVIEW_WEBHOOK_PREFIX)
        )
        if tradingview_delivery:
            try:
                request.state.verified_tradingview_source_ip = (
                    require_verified_tradingview_delivery(request)
                )
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )
            limiter: WebhookRateLimiter = (
                request.app.state.tradingview_rate_limiter
            )
            if not limiter.allow(
                request.url.path,
                request.state.verified_tradingview_source_ip,
                limit=get_settings().tradingview_webhook_requests_per_minute,
            ):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "webhook rate limit exceeded"},
                )
        if (
            not tradingview_delivery
            and get_settings().deployment_mode != "hosted-multi-user"
            and not _api_key_is_valid(request.headers.get("X-API-Key"))
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "valid API key required"},
            )
        if not tradingview_delivery:
            api_key = (
                request.headers.get("Authorization", "")
                if get_settings().deployment_mode == "hosted-multi-user"
                else request.headers.get("X-API-Key", "")
            )
            client_ip = request.client.host if request.client else "unknown"
            limiter = request.app.state.api_rate_limiter
            if not limiter.allow(
                hashlib.sha256(api_key.encode()).hexdigest(),
                client_ip,
                limit=get_settings().api_requests_per_minute,
            ):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "API rate limit exceeded"},
                )
        maximum = (
            get_settings().tradingview_webhook_max_request_bytes
            if tradingview_delivery
            else get_settings().api_max_request_bytes
        )
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "invalid Content-Length"},
                )
            if declared_length < 0:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "invalid Content-Length"},
                )
            if declared_length > maximum:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "API request body exceeds configured limit"},
                )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > maximum:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "API request body exceeds configured limit"},
                )
        request._body = bytes(body)  # noqa: SLF001 - replay exact bounded bytes downstream.
        request.state.body_sha256 = hashlib.sha256(request._body).hexdigest()
    return await call_next(request)


@app.middleware("http")
async def authenticate_hosted_principal(request: Request, call_next):
    """Authenticate and bind one exact tenant before any hosted database dependency."""
    settings = get_settings()
    if (
        settings.deployment_mode != "hosted-multi-user"
        or not request.url.path.startswith("/api/")
    ):
        return await call_next(request)
    if request.url.path.startswith(TRADINGVIEW_WEBHOOK_PREFIX):
        return JSONResponse(status_code=404, content={"detail": "not found"})
    workspace_text = request.headers.get("X-Workspace-ID", "")
    account_text = request.headers.get("X-Account-ID", "")
    authorization = request.headers.get("Authorization", "")
    try:
        scope = RequestScope(
            workspace_id=uuid.UUID(workspace_text),
            account_id=uuid.UUID(account_text),
        )
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=401,
            content={"detail": "authenticated workspace/account scope required"},
        )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return JSONResponse(
            status_code=401,
            content={"detail": "valid bearer principal required"},
        )
    with SessionLocal() as auth_db:
        principal = authenticate_principal(
            auth_db,
            bearer_token=token,
            scope=scope,
        )
    if principal is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "principal is not authorized for this account"},
        )
    if request.method in MUTATING_METHODS and principal.role not in {"trader", "admin"}:
        return JSONResponse(
            status_code=403,
            content={"detail": "principal role does not permit mutations"},
        )
    request.state.principal = principal
    with bind_database_scope(scope):
        return await call_next(request)


DatabaseSession = Annotated[Session, Depends(get_db)]
ImageUpload = Annotated[UploadFile, File()]
ChartContext = Annotated[str, Form()]
ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]
ConfirmationHeader = Annotated[
    str | None,
    Header(alias="X-Trader-Confirmation"),
]
StrategyVersionHeader = Annotated[
    uuid.UUID | None,
    Header(alias="X-Strategy-Version"),
]
WorkspaceHeader = Annotated[
    uuid.UUID | None,
    Header(alias="X-Workspace-ID"),
]
AccountHeader = Annotated[
    uuid.UUID | None,
    Header(alias="X-Account-ID"),
]


@dataclass(frozen=True)
class ConfirmationRecord:
    method: str
    path: str
    body_sha256: str
    workspace_id: str
    account_id: str
    principal_id: str | None
    expires_at: float


class ConfirmationStore:
    """Keep bounded, one-time request authorizations in process memory."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        maximum_pending: int = 256,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.maximum_pending = maximum_pending
        self._clock = clock or time.monotonic
        self._records: dict[str, ConfirmationRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        method: str,
        path: str,
        body_sha256: str,
        scope: RequestScope,
        principal_id: uuid.UUID | None = None,
    ) -> str:
        now = self._clock()
        with self._lock:
            self._records = {
                key: value
                for key, value in self._records.items()
                if value.expires_at > now
            }
            if len(self._records) >= self.maximum_pending:
                raise RuntimeError("too many pending confirmations")
            token = secrets.token_urlsafe(32)
            self._records[self._token_hash(token)] = ConfirmationRecord(
                method=method,
                path=path,
                body_sha256=body_sha256,
                workspace_id=str(scope.workspace_id),
                account_id=str(scope.account_id),
                principal_id=str(principal_id) if principal_id is not None else None,
                expires_at=now + self.ttl_seconds,
            )
        return token

    def consume(
        self,
        token: str,
        *,
        method: str,
        path: str,
        body_sha256: str,
        scope: RequestScope,
        principal_id: uuid.UUID | None = None,
    ) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return False
        token_hash = self._token_hash(token)
        with self._lock:
            record = self._records.pop(token_hash, None)
        if record is None or record.expires_at <= self._clock():
            return False
        return (
            hmac.compare_digest(record.method, method)
            and hmac.compare_digest(record.path, path)
            and hmac.compare_digest(record.body_sha256, body_sha256)
            and hmac.compare_digest(record.workspace_id, str(scope.workspace_id))
            and hmac.compare_digest(record.account_id, str(scope.account_id))
            and hmac.compare_digest(
                record.principal_id or "",
                str(principal_id) if principal_id is not None else "",
            )
        )


class ConfirmationChallengeRequest(BaseModel):
    method: str = Field(min_length=3, max_length=8)
    path: str = Field(min_length=1, max_length=300)
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("method")
    @classmethod
    def supported_method(cls, value: str) -> str:
        method = value.upper()
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("confirmation method must mutate state")
        return method

    @field_validator("path")
    @classmethod
    def safe_api_path(cls, value: str) -> str:
        if (
            not value.startswith("/api/")
            or "?" in value
            or "#" in value
            or value == "/api/confirmations/challenge"
        ):
            raise ValueError("confirmation path must be one mutating API path")
        return value


class ConfirmationChallengeRead(BaseModel):
    token: str
    expires_in_seconds: int
    method: str
    path: str
    body_sha256: str


def get_runtime_policy(request: Request) -> PolicyEngine:
    return request.app.state.policy


RuntimePolicyDependency = Annotated[PolicyEngine, Depends(get_runtime_policy)]


def require_api_key(api_key: ApiKeyHeader = None) -> None:
    """Validate the local single-user API key (kept as a testable pure dependency)."""
    if not _api_key_is_valid(api_key):
        raise HTTPException(status_code=401, detail="valid API key required")


def require_api_authentication(
    request: Request,
    api_key: ApiKeyHeader = None,
) -> None:
    if get_settings().deployment_mode == "hosted-multi-user":
        if getattr(request.state, "principal", None) is None:
            raise HTTPException(status_code=401, detail="valid bearer principal required")
        return
    require_api_key(api_key)


def require_request_scope(
    db: DatabaseSession,
    workspace_id: WorkspaceHeader = None,
    account_id: AccountHeader = None,
) -> RequestScope:
    if workspace_id is None or account_id is None:
        raise HTTPException(
            status_code=428,
            detail="X-Workspace-ID and X-Account-ID are required",
        )
    scope = RequestScope(workspace_id=workspace_id, account_id=account_id)
    try:
        validate_scope(db, scope)
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail="workspace/account scope was not found",
        ) from exc
    return scope


def require_verified_tradingview_delivery(request: Request) -> str:
    """Trust verification assertions only from an explicitly trusted TLS proxy."""
    verified_source = getattr(
        request.state,
        "verified_tradingview_source_ip",
        None,
    )
    if verified_source is not None:
        return verified_source
    settings = get_settings()
    if not settings.tradingview_webhook_enabled:
        raise HTTPException(status_code=404, detail="not found")
    peer = request.client.host if request.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail="unverified TradingView delivery",
        ) from exc
    try:
        trusted_networks = trusted_proxy_networks(
            settings.tradingview_trusted_proxy_cidrs
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="TradingView trusted proxy configuration is invalid",
        ) from exc
    if not any(peer_ip in network for network in trusted_networks):
        raise HTTPException(status_code=401, detail="unverified TradingView delivery")

    verified = request.headers.get("X-TradingView-Webhook-Verified", "")
    rate_limited = request.headers.get(
        "X-TradingView-Rate-Limit-Verified",
        "",
    )
    identity = request.headers.get("X-TradingView-Client-Identity", "")
    source_ip_text = request.headers.get("X-TradingView-Source-IP", "")
    try:
        source_ip = str(ipaddress.ip_address(source_ip_text))
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail="unverified TradingView delivery",
        ) from exc
    if (
        verified != "true"
        or rate_limited != "true"
        or not hmac.compare_digest(identity, TRADINGVIEW_CERTIFICATE_IDENTITY)
        or source_ip not in TRADINGVIEW_SOURCE_IPS
    ):
        raise HTTPException(status_code=401, detail="unverified TradingView delivery")
    return source_ip


async def require_trader_confirmation(
    request: Request,
    scope: Annotated[RequestScope, Depends(require_request_scope)],
    confirmation: ConfirmationHeader = None,
) -> None:
    if not confirmation:
        raise HTTPException(
            status_code=428,
            detail="a one-time X-Trader-Confirmation challenge token is required",
        )
    store: ConfirmationStore = request.app.state.confirmations
    body_sha256 = getattr(request.state, "body_sha256", None)
    if body_sha256 is None:
        body_sha256 = hashlib.sha256(await request.body()).hexdigest()
    if not store.consume(
        confirmation,
        method=request.method,
        path=request.url.path,
        body_sha256=body_sha256,
        scope=scope,
        principal_id=(
            request.state.principal.id
            if getattr(request.state, "principal", None) is not None
            else None
        ),
    ):
        raise HTTPException(
            status_code=428,
            detail="confirmation is expired, already used, or bound to another request",
        )


def require_strategy_version(
    db: DatabaseSession,
    scope: Annotated[RequestScope, Depends(require_request_scope)],
    strategy_version: StrategyVersionHeader = None,
) -> uuid.UUID:
    if strategy_version is None:
        raise HTTPException(
            status_code=428,
            detail="X-Strategy-Version must select one immutable strategy version",
        )
    try:
        version = validate_strategy_scope(db, scope, strategy_version)
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail="strategy version was not found",
        ) from exc
    if version is None:
        raise HTTPException(status_code=404, detail="strategy version was not found")
    return strategy_version


ApiKeyDependency = Annotated[None, Depends(require_api_authentication)]
ConfirmationDependency = Annotated[None, Depends(require_trader_confirmation)]
StrategyVersionDependency = Annotated[uuid.UUID, Depends(require_strategy_version)]
ScopeDependency = Annotated[RequestScope, Depends(require_request_scope)]
TradingViewVerificationDependency = Annotated[
    str,
    Depends(require_verified_tradingview_delivery),
]


def authorize_api_call(
    policy: PolicyEngine,
    *,
    name: str,
    arguments: dict,
    mutating: bool = False,
    deterministic: bool = False,
) -> None:
    """Apply the startup policy before an API operation reaches its service."""
    policy.authorize_registered_action(
        ToolContext(
            name=name,
            arguments=arguments,
            mutating=mutating,
            deterministic=deterministic,
        )
    )


@contextmanager
def audit_api_mutation(
    db: Session,
    *,
    scope: RequestScope,
    action: str,
    arguments: dict,
):
    """Record confirmed API mutation outcomes even when its transaction rolls back."""
    audit = record_direct_cli_confirmation(
        db,
        scope=scope,
        action=action,
        arguments=arguments,
    )
    try:
        yield
    except BaseException as exc:
        db.rollback()
        complete_mutation_audit(db, audit.id, scope=scope, error=exc)
        raise
    else:
        complete_mutation_audit(db, audit.id, scope=scope)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health(policy: RuntimePolicyDependency) -> dict[str, str]:
    policy.assert_unchanged()
    return {"status": "ok"}


@app.post(
    TRADINGVIEW_WEBHOOK_PATH,
    response_model=TradingViewWebhookReceipt,
    status_code=202,
)
def receive_tradingview_alert(
    account_id: uuid.UUID,
    payload: TradingViewWebhookCreate,
    request: Request,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    verified_source_ip: TradingViewVerificationDependency,
) -> TradingViewWebhookReceipt:
    settings = get_settings()
    delivery_age = (
        datetime.now(UTC) - payload.sent_at.astimezone(UTC)
    ).total_seconds()
    if (
        delivery_age > settings.tradingview_webhook_max_delivery_age_seconds
        or delivery_age < -settings.tradingview_webhook_future_skew_seconds
    ):
        raise HTTPException(
            status_code=401,
            detail="invalid webhook authorization",
        )
    workspace = resolve_workspace(db, settings.trading_workspace)
    account = (
        resolve_account(db, workspace.id, account_id)
        if workspace is not None
        else None
    )
    if workspace is None or account is None:
        raise HTTPException(status_code=401, detail="invalid webhook authorization")
    if not tradingview_webhook_secret_is_valid(
        account,
        payload.webhook_secret.get_secret_value(),
    ):
        raise HTTPException(status_code=401, detail="invalid webhook authorization")
    scope = RequestScope(workspace_id=workspace.id, account_id=account.id)
    alert_payload = payload.alert()
    authorize_api_call(
        policy,
        name="ingest_tradingview_alert",
        arguments={
            "event_id": alert_payload.event_id,
            "symbol": alert_payload.symbol,
            "timeframe": alert_payload.timeframe,
            "market_time": alert_payload.market_time.isoformat(),
            "sent_at": payload.sent_at.isoformat(),
        },
        mutating=True,
    )
    audit_arguments = {
        "event_id": alert_payload.event_id,
        "symbol": alert_payload.symbol,
        "timeframe": alert_payload.timeframe,
        "market_time": alert_payload.market_time.isoformat(),
        "sent_at": payload.sent_at.isoformat(),
    }
    with audit_api_mutation(
        db,
        scope=scope,
        action="ingest_tradingview_alert",
        arguments=audit_arguments,
    ):
        try:
            alert, created = ingest_tradingview_alert(
                db,
                alert_payload,
                scope=scope,
                verified_source_ip=verified_source_ip,
            )
        except TradingViewEventConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TradingViewWebhookReceipt(
        accepted=True,
        duplicate=not created,
        alert_id=alert.id,
        event_id=alert.external_event_id,
    )


@app.get(
    "/api/integrations/tradingview/alerts",
    response_model=list[TradingViewAlertRead],
)
def list_tradingview_alerts(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 20,
) -> list:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    authorize_api_call(
        policy,
        name="get_recent_tradingview_alerts",
        arguments={
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
        },
    )
    return recent_tradingview_alerts(
        db,
        scope=scope,
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )


@app.post(
    "/api/confirmations/challenge",
    response_model=ConfirmationChallengeRead,
)
def create_confirmation_challenge(
    challenge: ConfirmationChallengeRequest,
    request: Request,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> ConfirmationChallengeRead:
    store: ConfirmationStore = request.app.state.confirmations
    try:
        token = store.issue(
            method=challenge.method,
            path=challenge.path,
            body_sha256=challenge.body_sha256,
            scope=scope,
            principal_id=(
                request.state.principal.id
                if getattr(request.state, "principal", None) is not None
                else None
            ),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return ConfirmationChallengeRead(
        token=token,
        expires_in_seconds=store.ttl_seconds,
        method=challenge.method,
        path=challenge.path,
        body_sha256=challenge.body_sha256,
    )


@app.post("/api/risk/position-size", response_model=PositionSizeResult)
def position_size(
    request: PositionSizeRequest,
    policy: RuntimePolicyDependency,
    _scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> PositionSizeResult:
    authorize_api_call(
        policy,
        name="calculate_position_size",
        arguments=request.model_dump(mode="json"),
        deterministic=True,
    )
    return calculate_position_size(request)


@app.post("/api/trades", response_model=TradePlanRead, status_code=201)
def create_trade(
    request: TradePlanCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    strategy_version: StrategyVersionDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
    _confirmation: ConfirmationDependency,
) -> TradePlan:
    authorize_api_call(
        policy,
        name="create_trade_plan",
        arguments=request.model_dump(mode="json"),
        mutating=True,
    )
    arguments = request.model_dump(mode="json")
    with audit_api_mutation(
        db, scope=scope, action="create_trade_plan", arguments=arguments
    ):
        return create_trade_plan(
            db,
            request,
            scope=scope,
            policy_hash=policy.content_hash,
            source="api",
            maximum_risk_percent=Decimal(
                str(get_settings().maximum_trade_risk_percent)
            ),
            playbook_version_id=strategy_version,
        )


@app.get("/api/trades", response_model=list[TradePlanRead])
def list_trades(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    strategy_version: StrategyVersionDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> list[TradePlan]:
    authorize_api_call(policy, name="list_trade_plans", arguments={})
    return list_trade_plans(
        db,
        scope=scope,
        playbook_version_id=strategy_version,
    )


@app.get("/api/trades/{trade_id}", response_model=TradePlanRead)
def get_trade(
    trade_id: uuid.UUID,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    strategy_version: StrategyVersionDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> TradePlan:
    authorize_api_call(
        policy,
        name="get_trade_plan",
        arguments={"trade_id": str(trade_id)},
    )
    try:
        return get_trade_plan(
            db,
            trade_id,
            scope=scope,
            playbook_version_id=strategy_version,
        )
    except TradeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/trades/{trade_id}/reflection", response_model=ReflectionRead, status_code=201)
def add_reflection(
    trade_id: uuid.UUID,
    request: ReflectionCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    strategy_version: StrategyVersionDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
    _confirmation: ConfirmationDependency,
) -> TradeReflection:
    authorize_api_call(
        policy,
        name="add_trade_reflection",
        arguments={"trade_id": str(trade_id), **request.model_dump(mode="json")},
        mutating=True,
    )
    arguments = {"trade_id": str(trade_id), **request.model_dump(mode="json")}
    with audit_api_mutation(
        db, scope=scope, action="add_trade_reflection", arguments=arguments
    ):
        try:
            return create_reflection(
                db,
                trade_id,
                request,
                scope=scope,
                playbook_version_id=strategy_version,
            )
        except TradeNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReflectionExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/charts/analyze", response_model=ChartAnalysis)
async def chart_analysis(
    image: ImageUpload,
    policy: RuntimePolicyDependency,
    db: DatabaseSession,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
    _confirmation: ConfirmationDependency,
    context: ChartContext = "",
    instrument: Annotated[str | None, Form()] = None,
    venue: Annotated[str | None, Form()] = None,
    timeframe: Annotated[str | None, Form()] = None,
    market_time: Annotated[str | None, Form()] = None,
    trade_plan_id: Annotated[uuid.UUID | None, Form()] = None,
    strategy_version: StrategyVersionHeader = None,
) -> ChartAnalysis:
    authorize_api_call(
        policy,
        name="analyze_chart",
        arguments={"content_type": image.content_type, "context": context},
        mutating=True,
    )
    allowed_types = {"image/png", "image/jpeg", "image/webp"}
    if image.content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="PNG, JPEG, or WebP required")
    image_bytes = await image.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="image exceeds 10 MB")
    if trade_plan_id is not None:
        if strategy_version is None:
            raise HTTPException(
                status_code=428,
                detail=(
                    "X-Strategy-Version is required when chart evidence is linked "
                    "to a trade"
                ),
            )
        try:
            get_trade_plan(
                db,
                trade_plan_id,
                scope=scope,
                playbook_version_id=strategy_version,
            )
        except TradeNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    observed_at = None
    if market_time is not None:
        try:
            observed_at = datetime.fromisoformat(market_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid market_time") from exc
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise HTTPException(
                status_code=422,
                detail="market_time must include a timezone",
            )
    settings = get_settings()
    provider = create_model_provider(settings)
    try:
        result = analyze_chart(
            image_bytes=image_bytes,
            content_type=image.content_type,
            user_context=context,
            settings=settings,
            provider=provider,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    with audit_api_mutation(
        db,
        scope=scope,
        action="analyze_chart",
        arguments={
            "content_type": image.content_type,
            "context": context,
            "instrument": instrument,
            "venue": venue,
            "timeframe": timeframe,
            "market_time": market_time,
            "trade_plan_id": str(trade_plan_id) if trade_plan_id else None,
        },
    ):
        record_chart_analysis(
            db,
            scope=scope,
            image_bytes=image_bytes,
            content_type=image.content_type,
            evidence_directory=settings.evidence_directory,
            analysis=result,
            provider=provider,
            policy_hash=policy.content_hash,
            prompt=SYSTEM_PROMPT,
            source="api",
            market_time=observed_at,
            instrument=instrument,
            venue=venue,
            timeframe=timeframe,
            trade_plan_id=trade_plan_id,
        )
    return result
