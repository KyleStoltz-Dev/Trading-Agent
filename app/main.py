import hmac
import uuid
from contextlib import asynccontextmanager
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
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import get_settings, secret_value
from app.db import get_db, upgrade_database
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
    if get_settings().database_auto_migrate:
        upgrade_database()
    yield


app = FastAPI(
    title="Trading Agent",
    version="0.1.0",
    description="Human-in-the-loop trading playbook and journal.",
    lifespan=lifespan,
)

DatabaseSession = Annotated[Session, Depends(get_db)]
ImageUpload = Annotated[UploadFile, File()]
ChartContext = Annotated[str, Form()]
ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]
ConfirmationHeader = Annotated[
    str | None,
    Header(alias="X-Trader-Confirmation"),
]


def get_runtime_policy(request: Request) -> PolicyEngine:
    return request.app.state.policy


RuntimePolicyDependency = Annotated[PolicyEngine, Depends(get_runtime_policy)]


def require_api_key(api_key: ApiKeyHeader = None) -> None:
    expected = secret_value(get_settings().trading_agent_api_key)
    if (
        expected is None
        or len(expected) < 32
        or api_key is None
        or not hmac.compare_digest(expected, api_key)
    ):
        raise HTTPException(status_code=401, detail="valid API key required")


def require_trader_confirmation(confirmation: ConfirmationHeader = None) -> None:
    if confirmation != "confirmed":
        raise HTTPException(
            status_code=428,
            detail="X-Trader-Confirmation: confirmed is required",
        )


ApiKeyDependency = Annotated[None, Depends(require_api_key)]
ConfirmationDependency = Annotated[None, Depends(require_trader_confirmation)]


def authorize_api_call(
    policy: PolicyEngine,
    *,
    name: str,
    arguments: dict,
    mutating: bool = False,
    deterministic: bool = False,
) -> None:
    """Apply the startup policy before an API operation reaches its service."""
    policy.authorize(
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
    )


@app.get("/api/trades", response_model=list[TradePlanRead])
def list_trades(
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
) -> list[TradePlan]:
    authorize_api_call(policy, name="list_trade_plans", arguments={})
    return list_trade_plans(db)


@app.get("/api/trades/{trade_id}", response_model=TradePlanRead)
def get_trade(
    trade_id: uuid.UUID,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
    _api_key: ApiKeyDependency,
) -> TradePlan:
    authorize_api_call(
        policy,
        name="get_trade_plan",
        arguments={"trade_id": str(trade_id)},
    )
    try:
        return get_trade_plan(db, trade_id)
    except TradeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/trades/{trade_id}/reflection", response_model=ReflectionRead, status_code=201)
def add_reflection(
    trade_id: uuid.UUID,
    request: ReflectionCreate,
    db: DatabaseSession,
    policy: RuntimePolicyDependency,
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
        return create_reflection(db, trade_id, request)
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
