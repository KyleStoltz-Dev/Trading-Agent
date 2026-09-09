import asyncio
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
from typing import Annotated, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings, secret_value
from app.connectors import BrokerConfigurationError
from app.connectors.alpaca import AlpacaConnectorError
from app.connectors.factory import create_broker_connector, create_market_data_connector
from app.connectors.kraken import KrakenConnectorError
from app.connectors.metatrader_bridge import MetaTraderBridgeError
from app.connectors.oanda import OandaConnectorError
from app.db import (
    SessionLocal,
    bind_database_scope,
    engine,
    get_db,
    upgrade_database,
    verify_hosted_rls,
)
from app.market_data.contracts import MarketInstrument
from app.models import BrokerConnection, TradePlan, TradeReflection, TradingAccount
from app.policy import PolicyEngine, ToolContext
from app.providers import ProviderConfigurationError, create_model_provider
from app.providers.openai_realtime import create_realtime_client_secret
from app.providers.subscription_provider import (
    claude_subscription_status,
    codex_subscription_status,
)
from app.schemas import (
    AgentContextRead,
    AgentMessageCreate,
    AgentMessageRead,
    AgentModelRead,
    AgentProviderCredentialWrite,
    AgentProviderRead,
    AgentSessionCreate,
    AgentSessionRead,
    BrokerPositionRead,
    BrokerStateRead,
    ChartAnalysis,
    ChatWebhookMessageRead,
    ChatWebhookReceipt,
    DashboardCustomizeRequest,
    DashboardCustomizeResponse,
    DiscordWebhookCreate,
    MarketCandleRead,
    MarketDataRead,
    MarketInstrumentCatalogRead,
    MarketInstrumentRead,
    MarketQuoteRead,
    PositionSizeRequest,
    PositionSizeResult,
    RealtimeClientSecretCreate,
    RealtimeClientSecretRead,
    RealtimeProviderRead,
    RealtimeUsageCreate,
    RealtimeUsageRead,
    ReflectionCreate,
    ReflectionRead,
    StrategySummary,
    TelegramWebhookCreate,
    TradePlanCreate,
    TradePlanRead,
    TradingViewAlertRead,
    TradingViewWebhookCreate,
    TradingViewWebhookReceipt,
)
from app.services.agent_gateway import (
    run_agent_turn,
    selectable_agent_models,
    start_agent_session,
)
from app.services.chart_analysis import SYSTEM_PROMPT, analyze_chart
from app.services.chat_webhooks import (
    ChatWebhookReplayError,
    ChatWebhookValidationError,
    chat_webhook_secret_is_valid,
    ingest_chat_webhook_message,
    recent_chat_webhooks,
)
from app.services.dashboard_customization import (
    DashboardCustomizationError,
    customize_dashboard_layout,
)
from app.services.evidence import record_chart_analysis
from app.services.journal import (
    ReflectionExistsError,
    TradeNotFoundError,
    create_reflection,
    create_trade_plan,
    get_trade_plan,
    list_trade_plans,
)
from app.services.model_credentials import (
    model_api_key_configured,
    resolve_model_credentials,
    store_model_api_key,
)
from app.services.principals import authenticate_principal
from app.services.realtime_usage import (
    RealtimeUsageConflictError,
    record_realtime_usage,
)
from app.services.risk import calculate_position_size
from app.services.secrets import SecretBackendError, validate_secret_backend
from app.services.strategy_workspace import list_strategy_summaries
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
    resolve_current_scope,
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


class DashboardBootstrapToken:
    """Consume one launcher-generated browser bootstrap token exactly once."""

    def __init__(self, token: str | None) -> None:
        self._token_hash = (
            hashlib.sha256(token.encode("utf-8")).digest() if token else None
        )
        self._consumed = False
        self._lock = threading.Lock()

    def consume(self, candidate: str | None) -> bool:
        if candidate is None or not re.fullmatch(r"[A-Za-z0-9_-]{43}", candidate):
            return False
        candidate_hash = hashlib.sha256(candidate.encode("utf-8")).digest()
        with self._lock:
            if self._consumed or self._token_hash is None:
                return False
            if not hmac.compare_digest(self._token_hash, candidate_hash):
                return False
            self._consumed = True
            return True


@asynccontextmanager
async def lifespan(application: FastAPI):
    with _market_instrument_cache_lock:
        _market_instrument_cache.clear()
    application.state.policy = PolicyEngine.load()
    application.state.confirmations = ConfirmationStore(
        ttl_seconds=get_settings().api_confirmation_ttl_seconds
    )
    application.state.tradingview_rate_limiter = WebhookRateLimiter()
    application.state.api_rate_limiter = WebhookRateLimiter()
    application.state.dashboard_bootstrap = DashboardBootstrapToken(
        secret_value(get_settings().trading_dashboard_bootstrap_token)
    )
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
DASHBOARD_SESSION_COOKIE = "trading_agent_dashboard_session"
TRADINGVIEW_WEBHOOK_PATH = "/api/webhooks/tradingview/{account_id}"
TRADINGVIEW_WEBHOOK_PREFIX = "/api/webhooks/tradingview/"
TELEGRAM_WEBHOOK_PATH = "/api/webhooks/telegram/{account_id}"
DISCORD_WEBHOOK_PATH = "/api/webhooks/discord/{account_id}"
CHAT_WEBHOOK_PREFIXES = (
    TRADINGVIEW_WEBHOOK_PREFIX,
    "/api/webhooks/telegram/",
    "/api/webhooks/discord/",
)
_market_instrument_cache: dict[
    tuple[str, str, str],
    tuple[float, str, tuple[MarketInstrument, ...]],
] = {}
_market_instrument_cache_lock = threading.Lock()


def _is_tradingview_webhook_path(path: str) -> bool:
    return path.startswith(TRADINGVIEW_WEBHOOK_PREFIX)


def _is_telegram_webhook_path(path: str) -> bool:
    return path.startswith("/api/webhooks/telegram/")


def _is_discord_webhook_path(path: str) -> bool:
    return path.startswith("/api/webhooks/discord/")
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


def _local_api_credential(request: Request) -> str | None:
    """Read a manual API key or the HttpOnly dashboard session cookie."""
    return request.headers.get("X-API-Key") or request.cookies.get(
        DASHBOARD_SESSION_COOKIE
    )


@app.middleware("http")
async def bind_confirmation_to_raw_body(
    request: Request,
    call_next,
):
    """Hash raw API mutation bytes before JSON or multipart parsing consumes them."""
    if request.method in MUTATING_METHODS and request.url.path.startswith("/api/"):
        is_dashboard_bootstrap = (
            request.method == "POST"
            and request.url.path == "/api/dashboard/session"
        )
        is_webhook_delivery = (
            request.method == "POST"
            and (
                _is_tradingview_webhook_path(request.url.path)
                or _is_telegram_webhook_path(request.url.path)
                or _is_discord_webhook_path(request.url.path)
            )
        )
        tradingview_delivery = request.method == "POST" and _is_tradingview_webhook_path(
            request.url.path
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
            not is_webhook_delivery
            and not is_dashboard_bootstrap
            and get_settings().deployment_mode != "hosted-multi-user"
            and not _api_key_is_valid(_local_api_credential(request))
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "valid API key required"},
            )
        if not is_webhook_delivery:
            api_key = (
                request.headers.get("Authorization", "")
                if get_settings().deployment_mode == "hosted-multi-user"
                else (
                    "dashboard-bootstrap"
                    if is_dashboard_bootstrap
                    else _local_api_credential(request) or ""
                )
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
            if is_webhook_delivery
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
    if (
        _is_tradingview_webhook_path(request.url.path)
        or _is_telegram_webhook_path(request.url.path)
        or _is_discord_webhook_path(request.url.path)
    ):
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
    require_api_key(api_key or request.cookies.get(DASHBOARD_SESSION_COOKIE))


def require_dashboard_bootstrap(
    request: Request,
    bootstrap_token: ApiKeyHeader = None,
) -> None:
    """Accept only the launcher's single-use local browser bootstrap token."""
    settings = get_settings()
    if (
        settings.deployment_mode != "local-single-user"
        or not settings.trading_dashboard_autoconnect
    ):
        raise HTTPException(status_code=404, detail="not found")
    store: DashboardBootstrapToken = request.app.state.dashboard_bootstrap
    if not store.consume(bootstrap_token):
        raise HTTPException(status_code=401, detail="invalid dashboard bootstrap token")


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


@app.post("/api/dashboard/session", status_code=204)
def create_dashboard_session(
    response: Response,
    policy: RuntimePolicyDependency,
    _bootstrap: Annotated[None, Depends(require_dashboard_bootstrap)],
) -> None:
    """Exchange the one-use browser fragment for an HttpOnly local session cookie."""
    policy.assert_unchanged()
    settings = get_settings()
    if settings.deployment_mode != "local-single-user":
        raise HTTPException(status_code=404, detail="not found")
    session_key = secret_value(settings.trading_agent_api_key)
    if session_key is None:
        raise HTTPException(status_code=503, detail="dashboard session is unavailable")
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        session_key,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )


@app.get("/api/agent/context", response_model=AgentContextRead)
def agent_context(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
) -> AgentContextRead:
    """Resolve Pippy's configured local account without weakening scoped routes."""
    authorize_api_call(
        policy,
        name="get_agent_context",
        arguments={},
    )
    settings = get_settings()
    if settings.deployment_mode != "local-single-user":
        raise HTTPException(
            status_code=404,
            detail="the configured agent context is available only in local mode",
        )
    try:
        scope = resolve_current_scope(
            db,
            workspace_reference=settings.trading_workspace,
            account_reference=settings.trading_account,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AgentContextRead(
        workspace_id=scope.workspace_id,
        account_id=scope.account_id,
        broker_provider=settings.broker_provider,
        news_provider=settings.news_provider,
    )


@app.get("/api/agent/models", response_model=list[AgentModelRead])
def agent_models(
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
) -> list[AgentModelRead]:
    """Expose Trading Agent's selectable model catalog to trusted local clients."""
    authorize_api_call(policy, name="list_agent_models", arguments={})
    return [
        AgentModelRead(
            provider=option.provider,
            model=option.model,
            label=option.label,
            location=option.location,
            available=option.available,
            selected=option.selected,
        )
        for option in selectable_agent_models(get_settings())
    ]


@app.get("/api/agent/providers", response_model=list[AgentProviderRead])
def agent_providers(
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
) -> list[AgentProviderRead]:
    """Report local and cloud brains without exposing credential material."""
    authorize_api_call(policy, name="list_agent_providers", arguments={})
    settings = get_settings()
    if settings.deployment_mode != "local-single-user":
        raise HTTPException(
            status_code=404,
            detail="provider setup is available only in local mode",
        )
    try:
        openai_configured = model_api_key_configured(settings, provider="openai")
        anthropic_configured = model_api_key_configured(settings, provider="anthropic")
    except SecretBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    storage = f"{settings.broker_secret_backend} credential vault"
    openai_subscription = codex_subscription_status()
    anthropic_subscription = claude_subscription_status()
    openai_uses_subscription = settings.openai_auth_mode == "subscription" or (
        settings.openai_auth_mode == "auto" and openai_subscription.ready
    )
    anthropic_uses_subscription = settings.anthropic_auth_mode == "subscription" or (
        settings.anthropic_auth_mode == "auto" and anthropic_subscription.ready
    )
    return [
        AgentProviderRead(
            provider="ollama",
            label="Local · Ollama",
            configured=True,
            location="local",
            credential_storage="No API key required",
            access_mode="local",
        ),
        AgentProviderRead(
            provider="openai",
            label=("ChatGPT subscription" if openai_uses_subscription else "OpenAI API"),
            configured=(
                openai_subscription.ready
                if openai_uses_subscription
                else openai_configured
            ),
            location="cloud",
            credential_storage=(
                "Codex sign-in" if openai_uses_subscription else storage
            ),
            access_mode="subscription" if openai_uses_subscription else "api",
        ),
        AgentProviderRead(
            provider="anthropic",
            label=("Claude subscription" if anthropic_uses_subscription else "Claude API"),
            configured=(
                anthropic_subscription.ready
                if anthropic_uses_subscription
                else anthropic_configured
            ),
            location="cloud",
            credential_storage=(
                "Claude Code sign-in" if anthropic_uses_subscription else storage
            ),
            access_mode="subscription" if anthropic_uses_subscription else "api",
        ),
    ]


@app.post(
    "/api/agent/providers/{provider}/credentials",
    response_model=AgentProviderRead,
)
def configure_agent_provider(
    provider: Literal["openai", "anthropic"],
    request: AgentProviderCredentialWrite,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
    _confirmation: ConfirmationDependency,
) -> AgentProviderRead:
    """Store a user-supplied model key in the configured operating-system vault."""
    authorize_api_call(
        policy,
        name="configure_agent_provider",
        arguments={"provider": provider},
        mutating=True,
    )
    settings = get_settings()
    if settings.deployment_mode != "local-single-user":
        raise HTTPException(
            status_code=404,
            detail="provider setup is available only in local mode",
        )
    arguments = {"provider": provider}
    with audit_api_mutation(
        db,
        scope=scope,
        action="configure_agent_provider",
        arguments=arguments,
    ):
        try:
            store_model_api_key(
                settings,
                provider=provider,
                api_key=request.api_key.get_secret_value(),
            )
        except SecretBackendError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AgentProviderRead(
        provider=provider,
        label="OpenAI API" if provider == "openai" else "Claude API",
        configured=True,
        location="cloud",
        credential_storage=f"{settings.broker_secret_backend} credential vault",
        access_mode="api",
    )


@app.get("/api/agent/realtime", response_model=RealtimeProviderRead)
def realtime_provider(
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
) -> RealtimeProviderRead:
    """Report whether the vault contains an API key usable by Pippy Realtime."""

    authorize_api_call(policy, name="get_realtime_provider", arguments={})
    settings = get_settings()
    try:
        configured = model_api_key_configured(settings, provider="openai")
    except SecretBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RealtimeProviderRead(
        configured=configured,
        model=settings.openai_realtime_model,
        credential_storage=f"{settings.broker_secret_backend} credential vault",
    )


@app.post(
    "/api/agent/realtime/client-secret",
    response_model=RealtimeClientSecretRead,
)
def issue_realtime_client_secret(
    request: RealtimeClientSecretCreate,
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
) -> RealtimeClientSecretRead:
    """Create a short-lived Realtime credential from the vault-held project key."""

    authorize_api_call(
        policy,
        name="create_realtime_client_secret",
        arguments={"voice": request.voice},
    )
    settings = get_settings()
    try:
        credentials = resolve_model_credentials(settings, provider="openai")
    except SecretBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if credentials is None:
        raise HTTPException(
            status_code=503,
            detail="An OpenAI API key is required for Realtime voice.",
        )
    try:
        payload = create_realtime_client_secret(
            api_key=credentials.api_key,
            model=settings.openai_realtime_model,
            voice=request.voice,
            safety_identifier=settings.openai_safety_identifier,
        )
    except (ProviderConfigurationError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RealtimeClientSecretRead(**payload)


@app.post("/api/agent/sessions", response_model=AgentSessionRead, status_code=201)
def create_agent_session(
    request: AgentSessionCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> AgentSessionRead:
    """Create the durable PostgreSQL conversation used by one Pippy session."""
    authorize_api_call(
        policy,
        name="start_agent_session",
        arguments={"name": request.name, "title": request.title},
        mutating=True,
    )
    arguments = {"name": request.name, "title": request.title}
    with audit_api_mutation(
        db,
        scope=scope,
        action="start_agent_session",
        arguments=arguments,
    ):
        try:
            session = start_agent_session(
                db,
                scope=scope,
                name=request.name,
                title=request.title,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentSessionRead(
        session_id=session.id,
        name=session.name,
        title=session.title,
    )


@app.post(
    "/api/agent/sessions/{session_id}/messages",
    response_model=AgentMessageRead,
)
def create_agent_message(
    session_id: uuid.UUID,
    request: AgentMessageCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> AgentMessageRead:
    """Run Pippy through the same provider, tools, policy, and audit path as chat."""
    authorize_api_call(
        policy,
        name="run_agent_turn",
        arguments={
            "session_id": str(session_id),
            "provider": request.provider,
            "model": request.model,
            "mode": request.mode,
        },
        mutating=True,
    )
    arguments = {
        "session_id": str(session_id),
        "provider": request.provider,
        "model": request.model,
        "mode": request.mode,
    }
    with audit_api_mutation(
        db,
        scope=scope,
        action="run_agent_turn",
        arguments=arguments,
    ):
        try:
            result = run_agent_turn(
                db,
                engine=engine,
                settings=get_settings(),
                policy=policy,
                scope=scope,
                session_id=session_id,
                message=request.message,
                provider_name=request.provider,
                model=request.model,
                mode=request.mode,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProviderConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AgentMessageRead(
        session_id=result.session_id,
        response=result.response,
        provider=result.provider,
        model=result.model,
        mode=result.mode,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        references=list(result.references),
    )


@app.post(
    "/api/agent/sessions/{session_id}/realtime-usage",
    response_model=RealtimeUsageRead,
)
def create_realtime_usage_event(
    session_id: uuid.UUID,
    request: RealtimeUsageCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> RealtimeUsageRead:
    """Persist one idempotent Realtime response usage event in PostgreSQL."""

    authorize_api_call(
        policy,
        name="record_realtime_usage",
        arguments={
            "session_id": str(session_id),
            "response_id": request.response_id,
            "model": request.model,
        },
        mutating=True,
        deterministic=True,
    )
    arguments = {
        "session_id": str(session_id),
        "response_id": request.response_id,
        "model": request.model,
    }
    with audit_api_mutation(
        db,
        scope=scope,
        action="record_realtime_usage",
        arguments=arguments,
    ):
        try:
            event = record_realtime_usage(
                db,
                scope=scope,
                session_id=session_id,
                **request.model_dump(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RealtimeUsageConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RealtimeUsageRead(
        id=event.id,
        session_id=event.session_id,
        response_id=event.response_id,
        model=event.model,
        input_text_tokens=event.input_text_tokens,
        input_audio_tokens=event.input_audio_tokens,
        cached_text_tokens=event.cached_text_tokens,
        cached_audio_tokens=event.cached_audio_tokens,
        output_text_tokens=event.output_text_tokens,
        output_audio_tokens=event.output_audio_tokens,
        estimated_cost_usd=event.estimated_cost_usd,
        created_at=event.created_at,
    )


@app.get("/api/strategies", response_model=list[StrategySummary])
def strategy_catalog(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> list[StrategySummary]:
    """List only the latest immutable version of each scoped strategy."""
    authorize_api_call(
        policy,
        name="list_strategies",
        arguments={},
    )
    return list_strategy_summaries(db, scope=scope)


@app.post("/api/dashboard/customize", response_model=DashboardCustomizeResponse)
def customize_dashboard(
    request: DashboardCustomizeRequest,
    policy: RuntimePolicyDependency,
    _scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> DashboardCustomizeResponse:
    authorize_api_call(
        policy,
        name="customize_dashboard",
        arguments={"request": request.request},
    )
    provider = create_model_provider(get_settings())
    try:
        spec, summary = customize_dashboard_layout(provider, request)
    except DashboardCustomizationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return DashboardCustomizeResponse(
        spec=spec,
        summary=summary,
        provider=provider.name,
        model=provider.model,
    )


@app.get("/api/broker-state", response_model=BrokerStateRead)
def broker_state(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
) -> BrokerStateRead:
    authorize_api_call(policy, name="get_broker_state", arguments={})

    async def fetch_state() -> BrokerStateRead:
        connector = None
        try:
            settings = get_settings()
            account = db.scalar(
                select(TradingAccount).where(
                    TradingAccount.workspace_id == scope.workspace_id,
                    TradingAccount.id == scope.account_id,
                )
            )
            if account is None:
                raise BrokerConfigurationError(
                    "the selected trading account was not found"
                )
            if settings.broker_provider == "oanda":
                provider = "oanda-v20"
            elif settings.broker_provider == "metatrader":
                provider = f"metatrader-{settings.metatrader_platform}-bridge"
            else:
                raise BrokerConfigurationError(
                    "select a supported read-only broker before loading account state"
                )
            connection = db.scalar(
                select(BrokerConnection).where(
                    BrokerConnection.workspace_id == scope.workspace_id,
                    BrokerConnection.account_id == scope.account_id,
                    BrokerConnection.provider == provider,
                )
            )
            connector = create_broker_connector(
                settings,
                account=account,
                connection=connection,
            )
            account, positions = await asyncio.gather(
                connector.account(),
                connector.positions(),
            )
        except BrokerConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OandaConnectorError, MetaTraderBridgeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            if connector is not None:
                await connector.aclose()
        return BrokerStateRead(
            provider=account.source,
            currency=account.currency,
            balance=account.balance,
            equity=account.equity,
            margin_used=account.margin_used,
            margin_available=account.margin_available,
            retrieved_at=account.retrieved_at,
            positions=[
                BrokerPositionRead(
                    external_id=item.external_id,
                    instrument=item.instrument,
                    net_quantity=item.net_quantity,
                    average_price=item.average_price,
                    unrealized_pnl=item.unrealized_pnl,
                    market_time=item.market_time,
                )
                for item in positions
            ],
        )

    return asyncio.run(fetch_state())


@app.get("/api/market-data", response_model=MarketDataRead)
def market_data(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
    workspace_id: WorkspaceHeader = None,
    account_id: AccountHeader = None,
    provider: str = Query(default="oanda", description="market data provider"),
    instrument: str = Query(default="XAU_USD", description="provider symbol"),
    timeframe: str = Query(default="H4", description="market timeframe"),
    count: int = Query(default=100, ge=1, le=5000),
) -> MarketDataRead:
    authorize_api_call(
        policy,
        name="get_market_data",
        arguments={
            "provider": provider,
            "instrument": instrument,
            "timeframe": timeframe,
            "count": count,
        },
    )
    async def fetch_data() -> MarketDataRead:
        connector = None
        try:
            settings = get_settings()
            normalized_provider = provider.strip().casefold()
            oanda_provider = normalized_provider in {"oanda", "oanda-v20", "oanda-v2"}
            metatrader_provider = normalized_provider in {
                "metatrader",
                "mt4",
                "mt5",
                "metatrader-mt4-bridge",
                "metatrader-mt5-bridge",
            }
            scoped_broker = metatrader_provider or (
                oanda_provider and (workspace_id is not None or account_id is not None)
            )
            if scoped_broker:
                if workspace_id is None or account_id is None:
                    raise HTTPException(
                        status_code=428,
                        detail=(
                            "both X-Workspace-ID and X-Account-ID are required to use "
                            "a saved broker connection"
                        ),
                    )
                scope = RequestScope(workspace_id=workspace_id, account_id=account_id)
                try:
                    account = validate_scope(db, scope)
                except LookupError as exc:
                    raise HTTPException(
                        status_code=404,
                        detail="workspace/account scope was not found",
                    ) from exc
                connection_provider = (
                    f"metatrader-{settings.metatrader_platform}-bridge"
                    if metatrader_provider
                    else "oanda-v20"
                )
                connection = db.scalar(
                    select(BrokerConnection).where(
                        BrokerConnection.workspace_id == scope.workspace_id,
                        BrokerConnection.account_id == scope.account_id,
                        BrokerConnection.provider == connection_provider,
                    )
                )
                connector = create_broker_connector(
                    settings,
                    account=account,
                    connection=connection,
                )
            else:
                connector = create_market_data_connector(settings, provider)
            quote = await connector.latest_quote(instrument)
            candles = await connector.candles(instrument, timeframe, count=count)
        except BrokerConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (
            OandaConnectorError,
            KrakenConnectorError,
            AlpacaConnectorError,
            MetaTraderBridgeError,
        ) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - provider-specific implementation detail
            raise HTTPException(
                status_code=503,
                detail="market data provider failed unexpectedly",
            ) from exc
        finally:
            if connector is not None:
                await connector.aclose()
        return MarketDataRead(
            provider=connector.name,
            instrument=instrument,
            timeframe=timeframe,
            quote=MarketQuoteRead(
                instrument=quote.instrument,
                bid=quote.bid,
                ask=quote.ask,
                spread=quote.spread,
                market_time=quote.market_time,
                retrieved_at=quote.retrieved_at,
                source=quote.source,
                venue=quote.venue,
            ),
            candles=[
                MarketCandleRead(
                    instrument=item.instrument,
                    timeframe=item.timeframe,
                    started_at=item.started_at,
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=item.volume,
                    complete=item.complete,
                    retrieved_at=item.retrieved_at,
                    source=item.source,
                    venue=item.venue,
                )
                for item in candles
            ],
        )

    return asyncio.run(fetch_data())


@app.get("/api/market-instruments", response_model=MarketInstrumentCatalogRead)
def market_instruments(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
    workspace_id: WorkspaceHeader = None,
    account_id: AccountHeader = None,
    provider: str = Query(default="oanda", description="market data provider"),
    query: str = Query(default="", max_length=80, description="symbol or name search"),
    limit: int = Query(default=50, ge=1, le=100),
) -> MarketInstrumentCatalogRead:
    """Return the real catalog exposed by the selected provider or broker account."""
    authorize_api_call(
        policy,
        name="list_market_instruments",
        arguments={"provider": provider, "query": query, "limit": limit},
    )

    async def fetch_instruments() -> MarketInstrumentCatalogRead:
        connector = None
        normalized_provider = provider.strip().casefold()
        cache_key = (
            normalized_provider,
            str(workspace_id or "global"),
            str(account_id or "global"),
        )
        now = time.monotonic()
        with _market_instrument_cache_lock:
            cached = _market_instrument_cache.get(cache_key)
            if cached is not None and now - cached[0] < 60:
                provider_name, items = cached[1], cached[2]
            else:
                provider_name, items = "", ()
        try:
            if not items:
                settings = get_settings()
                oanda_provider = normalized_provider in {
                    "oanda",
                    "oanda-v20",
                    "oanda-v2",
                }
                metatrader_provider = normalized_provider in {
                    "metatrader",
                    "mt4",
                    "mt5",
                    "metatrader-mt4-bridge",
                    "metatrader-mt5-bridge",
                }
                scoped_broker = metatrader_provider or (
                    oanda_provider and (workspace_id is not None or account_id is not None)
                )
                if scoped_broker:
                    if workspace_id is None or account_id is None:
                        raise HTTPException(
                            status_code=428,
                            detail=(
                                "both X-Workspace-ID and X-Account-ID are required to use "
                                "a saved broker connection"
                            ),
                        )
                    scope = RequestScope(workspace_id=workspace_id, account_id=account_id)
                    try:
                        account = validate_scope(db, scope)
                    except LookupError as exc:
                        raise HTTPException(
                            status_code=404,
                            detail="workspace/account scope was not found",
                        ) from exc
                    connection_provider = (
                        f"metatrader-{settings.metatrader_platform}-bridge"
                        if metatrader_provider
                        else "oanda-v20"
                    )
                    connection = db.scalar(
                        select(BrokerConnection).where(
                            BrokerConnection.workspace_id == scope.workspace_id,
                            BrokerConnection.account_id == scope.account_id,
                            BrokerConnection.provider == connection_provider,
                        )
                    )
                    connector = create_broker_connector(
                        settings,
                        account=account,
                        connection=connection,
                    )
                else:
                    connector = create_market_data_connector(settings, provider)
                items = tuple(await connector.instruments())
                provider_name = connector.name
                with _market_instrument_cache_lock:
                    if len(_market_instrument_cache) >= 32:
                        oldest = min(
                            _market_instrument_cache,
                            key=lambda key: _market_instrument_cache[key][0],
                        )
                        _market_instrument_cache.pop(oldest, None)
                    _market_instrument_cache[cache_key] = (
                        time.monotonic(),
                        provider_name,
                        items,
                    )
        except BrokerConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (
            OandaConnectorError,
            KrakenConnectorError,
            AlpacaConnectorError,
            MetaTraderBridgeError,
        ) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - provider-specific detail
            raise HTTPException(
                status_code=503,
                detail="market instrument catalog failed unexpectedly",
            ) from exc
        finally:
            if connector is not None:
                await connector.aclose()
        normalized_query = query.strip().casefold()
        filtered = sorted(
            (
                item
                for item in items
                if not normalized_query
                or normalized_query in item.symbol.casefold()
                or normalized_query in item.display_name.casefold()
                or normalized_query in item.venue.casefold()
                or normalized_query in item.asset_class.casefold()
            ),
            key=lambda candidate: candidate.symbol,
        )
        return MarketInstrumentCatalogRead(
            provider=provider_name,
            retrieved_at=datetime.now(UTC),
            total=len(filtered),
            has_more=len(filtered) > limit,
            instruments=[
                MarketInstrumentRead(
                    symbol=item.symbol,
                    display_name=item.display_name,
                    asset_class=item.asset_class,
                    source=item.source,
                    venue=item.venue,
                    tradable=item.tradable,
                )
                for item in filtered[:limit]
            ],
        )

    return asyncio.run(fetch_instruments())


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
    TELEGRAM_WEBHOOK_PATH,
    response_model=ChatWebhookReceipt,
    status_code=202,
)
def receive_telegram_webhook(
    account_id: uuid.UUID,
    payload: TelegramWebhookCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
) -> ChatWebhookReceipt:
    settings = get_settings()
    workspace = resolve_workspace(db, settings.trading_workspace)
    account = (
        resolve_account(db, workspace.id, account_id)
        if workspace is not None
        else None
    )
    if workspace is None or account is None:
        raise HTTPException(status_code=401, detail="invalid webhook authorization")
    if not chat_webhook_secret_is_valid(
        account,
        platform="telegram",
        candidate=payload.webhook_secret.get_secret_value(),
    ):
        raise HTTPException(status_code=401, detail="invalid webhook authorization")
    scope = RequestScope(workspace_id=workspace.id, account_id=account.id)
    authorize_api_call(
        policy,
        name="ingest_chat_webhook_message",
        arguments={"platform": "telegram"},
        mutating=True,
    )
    with audit_api_mutation(
        db,
        scope=scope,
        action="ingest_chat_webhook_message",
        arguments={"platform": "telegram"},
    ):
        try:
            message, created = ingest_chat_webhook_message(
                db,
                payload=payload.payload,
                platform="telegram",
                scope=scope,
            )
        except ChatWebhookValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ChatWebhookReplayError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ChatWebhookReceipt(
        accepted=True,
        duplicate=not created,
        platform="telegram",
        message_id=message.id,
        external_message_id=message.external_message_id,
    )


@app.post(
    DISCORD_WEBHOOK_PATH,
    response_model=ChatWebhookReceipt,
    status_code=202,
)
def receive_discord_webhook(
    account_id: uuid.UUID,
    payload: DiscordWebhookCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
) -> ChatWebhookReceipt:
    settings = get_settings()
    workspace = resolve_workspace(db, settings.trading_workspace)
    account = (
        resolve_account(db, workspace.id, account_id)
        if workspace is not None
        else None
    )
    if workspace is None or account is None:
        raise HTTPException(status_code=401, detail="invalid webhook authorization")
    if not chat_webhook_secret_is_valid(
        account,
        platform="discord",
        candidate=payload.webhook_secret.get_secret_value(),
    ):
        raise HTTPException(status_code=401, detail="invalid webhook authorization")
    scope = RequestScope(workspace_id=workspace.id, account_id=account.id)
    authorize_api_call(
        policy,
        name="ingest_chat_webhook_message",
        arguments={"platform": "discord"},
        mutating=True,
    )
    with audit_api_mutation(
        db,
        scope=scope,
        action="ingest_chat_webhook_message",
        arguments={"platform": "discord"},
    ):
        try:
            message, created = ingest_chat_webhook_message(
                db,
                payload=payload.payload,
                platform="discord",
                scope=scope,
            )
        except ChatWebhookValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ChatWebhookReplayError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ChatWebhookReceipt(
        accepted=True,
        duplicate=not created,
        platform="discord",
        message_id=message.id,
        external_message_id=message.external_message_id,
    )


@app.get("/api/integrations/chat/messages", response_model=list[ChatWebhookMessageRead])
def list_chat_webhook_messages(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    scope: ScopeDependency,
    _api_key: ApiKeyDependency,
    platform: str | None = None,
    limit: int = 20,
) -> list:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    if platform is not None and platform.lower() not in {"telegram", "discord"}:
        raise HTTPException(
            status_code=422,
            detail="platform must be 'telegram' or 'discord'",
        )
    authorize_api_call(
        policy,
        name="get_recent_chat_webhooks",
        arguments={
            "platform": platform,
            "limit": limit,
        },
    )
    return recent_chat_webhooks(
        db,
        scope=scope,
        platform=platform,
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
