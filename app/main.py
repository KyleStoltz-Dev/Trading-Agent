import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
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
from app.db import get_db, upgrade_database
from app.models import PlaybookVersion, TradePlan, TradeReflection
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
from app.services.risk import calculate_position_size


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.policy = PolicyEngine.load()
    application.state.confirmations = ConfirmationStore(
        ttl_seconds=get_settings().api_confirmation_ttl_seconds
    )
    if get_settings().database_auto_migrate:
        upgrade_database()
    yield


app = FastAPI(
    title="Trading Agent",
    version="0.1.0",
    description="Human-in-the-loop trading playbook and journal.",
    lifespan=lifespan,
)

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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
        if not _api_key_is_valid(request.headers.get("X-API-Key")):
            return JSONResponse(
                status_code=401,
                content={"detail": "valid API key required"},
            )
        maximum = get_settings().api_max_request_bytes
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


@dataclass(frozen=True)
class ConfirmationRecord:
    method: str
    path: str
    body_sha256: str
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

    def issue(self, *, method: str, path: str, body_sha256: str) -> str:
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
    if not _api_key_is_valid(api_key):
        raise HTTPException(status_code=401, detail="valid API key required")


async def require_trader_confirmation(
    request: Request,
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
    ):
        raise HTTPException(
            status_code=428,
            detail="confirmation is expired, already used, or bound to another request",
        )


def require_strategy_version(
    db: DatabaseSession,
    strategy_version: StrategyVersionHeader = None,
) -> uuid.UUID:
    if strategy_version is None:
        raise HTTPException(
            status_code=428,
            detail="X-Strategy-Version must select one immutable strategy version",
        )
    if db.get(PlaybookVersion, strategy_version) is None:
        raise HTTPException(
            status_code=404,
            detail="strategy version was not found",
        )
    return strategy_version


ApiKeyDependency = Annotated[None, Depends(require_api_key)]
ConfirmationDependency = Annotated[None, Depends(require_trader_confirmation)]
StrategyVersionDependency = Annotated[uuid.UUID, Depends(require_strategy_version)]


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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health(policy: RuntimePolicyDependency) -> dict[str, str]:
    policy.assert_unchanged()
    return {"status": "ok"}


@app.post(
    "/api/confirmations/challenge",
    response_model=ConfirmationChallengeRead,
)
def create_confirmation_challenge(
    challenge: ConfirmationChallengeRequest,
    request: Request,
    _api_key: ApiKeyDependency,
) -> ConfirmationChallengeRead:
    store: ConfirmationStore = request.app.state.confirmations
    try:
        token = store.issue(
            method=challenge.method,
            path=challenge.path,
            body_sha256=challenge.body_sha256,
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
    _api_key: ApiKeyDependency,
    _confirmation: ConfirmationDependency,
) -> TradePlan:
    authorize_api_call(
        policy,
        name="create_trade_plan",
        arguments=request.model_dump(mode="json"),
        mutating=True,
    )
    return create_trade_plan(
        db,
        request,
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
    _api_key: ApiKeyDependency,
) -> list[TradePlan]:
    authorize_api_call(policy, name="list_trade_plans", arguments={})
    return list_trade_plans(db, playbook_version_id=strategy_version)


@app.get("/api/trades/{trade_id}", response_model=TradePlanRead)
def get_trade(
    trade_id: uuid.UUID,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    strategy_version: StrategyVersionDependency,
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
    _api_key: ApiKeyDependency,
    _confirmation: ConfirmationDependency,
) -> TradeReflection:
    authorize_api_call(
        policy,
        name="add_trade_reflection",
        arguments={"trade_id": str(trade_id), **request.model_dump(mode="json")},
        mutating=True,
    )
    try:
        return create_reflection(
            db,
            trade_id,
            request,
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
    record_chart_analysis(
        db,
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
