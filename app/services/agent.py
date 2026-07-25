import asyncio
import json
import mimetypes
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.connectors import create_news_connector, create_oanda_connector
from app.policy import ExecutionHooks, PolicyEngine, policy_wrapped_executor
from app.providers import ModelProvider, create_model_provider
from app.routing import AgentMode, ModelRoute, route_model
from app.schemas import (
    BrokerPositionSizeRequest,
    PositionSizeRequest,
    ReflectionCreate,
    ReflectionRead,
    TradePlanCreate,
    TradePlanRead,
)
from app.services.catalog import active_instrument_specification
from app.services.chart_analysis import SYSTEM_PROMPT, analyze_chart
from app.services.evidence import record_chart_analysis
from app.services.health import check_health
from app.services.journal import (
    create_reflection,
    create_trade_plan,
    get_trade_plan,
    list_trade_plans,
)
from app.services.risk import calculate_broker_position_size, calculate_position_size

ConfirmMutation = Callable[[str, dict[str, Any]], bool]

AGENT_INSTRUCTIONS = """
You are Trading Agent, a journal-first decision-support assistant for a discretionary trader.
Help organize evidence, define context and trigger separately, calculate risk, record plans,
and review execution. Treat Wyckoff and smart-money terms as hypotheses until operationally
defined and supported by a reviewed sample.

Never invent a live price, timestamp, news event, fill, indicator, or chart detail. Never
promise an outcome, select authoritative position size yourself, or imply that confidence
increases permissible risk. Use the deterministic risk tool for calculations. Use journal
tools when relevant, but every journal mutation requires the trader's terminal confirmation.
There are no broker execution tools. State missing capabilities plainly.

When analyzing a local image path, use analyze_chart. Keep process quality separate from
outcome quality. This is decision support, not individualized financial advice.
""".strip()


def _object_schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS = [
    {
        "type": "function",
        "name": "calculate_position_size",
        "description": "Calculate risk amount, quantity, and planned R deterministically.",
        "strict": True,
        "parameters": _object_schema(
            {
                "account_equity": {"type": "string"},
                "risk_percent": {"type": "string"},
                "entry": {"type": "string"},
                "stop": {"type": "string"},
                "target": {"type": ["string", "null"]},
                "value_per_price_unit": {"type": "string"},
            },
            [
                "account_equity",
                "risk_percent",
                "entry",
                "stop",
                "target",
                "value_per_price_unit",
            ],
        ),
    },
    {
        "type": "function",
        "name": "calculate_broker_position_size",
        "description": (
            "Calculate authoritative quantity from a stored broker contract, costs, "
            "margin, quantity step, and configured risk ceiling."
        ),
        "strict": True,
        "parameters": _object_schema(
            {
                "provider": {"type": "string"},
                "symbol": {"type": "string"},
                "account_equity": {"type": "string"},
                "available_margin": {"type": ["string", "null"]},
                "risk_percent": {"type": "string"},
                "entry": {"type": "string"},
                "stop": {"type": "string"},
                "target": {"type": ["string", "null"]},
                "conversion_rate_to_account": {"type": "string"},
                "estimated_slippage": {"type": "string"},
            },
            [
                "provider",
                "symbol",
                "account_equity",
                "available_margin",
                "risk_percent",
                "entry",
                "stop",
                "target",
                "conversion_rate_to_account",
                "estimated_slippage",
            ],
        ),
    },
    {
        "type": "function",
        "name": "list_trade_plans",
        "description": "List recent journaled trade plans.",
        "strict": True,
        "parameters": _object_schema(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            ["limit"],
        ),
    },
    {
        "type": "function",
        "name": "get_trade_plan",
        "description": "Retrieve one journaled trade plan by UUID.",
        "strict": True,
        "parameters": _object_schema({"trade_id": {"type": "string"}}, ["trade_id"]),
    },
    {
        "type": "function",
        "name": "create_trade_plan",
        "description": "Journal a trade plan after terminal confirmation.",
        "strict": True,
        "parameters": _object_schema(
            {
                "instrument": {"type": "string"},
                "venue": {"type": ["string", "null"]},
                "direction": {"type": "string", "enum": ["long", "short"]},
                "setup_name": {"type": "string"},
                "regime": {"type": ["string", "null"]},
                "session_name": {"type": ["string", "null"]},
                "market_time": {"type": ["string", "null"]},
                "context_timeframe": {"type": "string"},
                "trigger_timeframe": {"type": "string"},
                "entry": {"type": "string"},
                "stop": {"type": "string"},
                "target": {"type": "string"},
                "account_equity": {"type": "string"},
                "risk_percent": {"type": "string"},
                "value_per_price_unit": {"type": "string"},
                "thesis": {"type": "string"},
                "invalidation": {"type": "string"},
                "observations": {"type": "array", "items": {"type": "string"}},
                "interpretations": {"type": "array", "items": {"type": "string"}},
                "sizing_provider": {"type": ["string", "null"]},
                "sizing_symbol": {"type": ["string", "null"]},
                "available_margin": {"type": ["string", "null"]},
                "conversion_rate_to_account": {"type": "string"},
                "estimated_slippage": {"type": "string"},
            },
            [
                "instrument",
                "venue",
                "direction",
                "setup_name",
                "regime",
                "session_name",
                "market_time",
                "context_timeframe",
                "trigger_timeframe",
                "entry",
                "stop",
                "target",
                "account_equity",
                "risk_percent",
                "value_per_price_unit",
                "thesis",
                "invalidation",
                "observations",
                "interpretations",
                "sizing_provider",
                "sizing_symbol",
                "available_margin",
                "conversion_rate_to_account",
                "estimated_slippage",
            ],
        ),
    },
    {
        "type": "function",
        "name": "add_trade_reflection",
        "description": "Add the one post-trade reflection for a journaled trade.",
        "strict": True,
        "parameters": _object_schema(
            {
                "trade_id": {"type": "string"},
                "exit_average": {"type": "string"},
                "realized_pnl": {"type": "string"},
                "execution_grade": {"type": "string", "enum": ["A", "B", "C", "D", "F"]},
                "rule_adherence": {
                    "type": "array",
                    "items": _object_schema(
                        {
                            "rule": {"type": "string"},
                            "followed": {"type": "boolean"},
                            "note": {"type": ["string", "null"]},
                        },
                        ["rule", "followed", "note"],
                    ),
                },
                "emotion_before": {"type": ["string", "null"]},
                "emotion_during": {"type": ["string", "null"]},
                "emotion_after": {"type": ["string", "null"]},
                "notes": {"type": "string"},
            },
            [
                "trade_id",
                "exit_average",
                "realized_pnl",
                "execution_grade",
                "rule_adherence",
                "emotion_before",
                "emotion_during",
                "emotion_after",
                "notes",
            ],
        ),
    },
    {
        "type": "function",
        "name": "analyze_chart",
        "description": "Analyze a PNG, JPEG, or WebP chart at a local path.",
        "strict": True,
        "parameters": _object_schema(
            {
                "image_path": {"type": "string"},
                "context": {"type": "string"},
            },
            ["image_path", "context"],
        ),
    },
    {
        "type": "function",
        "name": "get_system_health",
        "description": "Check configuration, OpenAI credentials, and database connectivity.",
        "strict": True,
        "parameters": _object_schema({}, []),
    },
    {
        "type": "function",
        "name": "get_live_quote",
        "description": "Get one timestamped current quote from the configured OANDA feed.",
        "strict": True,
        "parameters": _object_schema(
            {"instrument": {"type": "string"}},
            ["instrument"],
        ),
    },
    {
        "type": "function",
        "name": "get_recent_candles",
        "description": "Get timestamped recent OANDA candles without persisting every update.",
        "strict": True,
        "parameters": _object_schema(
            {
                "instrument": {"type": "string"},
                "timeframe": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["instrument", "timeframe", "count"],
        ),
    },
    {
        "type": "function",
        "name": "get_broker_state",
        "description": (
            "Get read-only account totals and open positions without account identifiers."
        ),
        "strict": True,
        "parameters": _object_schema({}, []),
    },
    {
        "type": "function",
        "name": "get_market_news",
        "description": "Get timestamped economic news metadata and provider summaries.",
        "strict": True,
        "parameters": _object_schema(
            {
                "country": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["country", "limit"],
        ),
    },
    {
        "type": "function",
        "name": "get_economic_calendar",
        "description": "Get scheduled economic events with importance and source timestamps.",
        "strict": True,
        "parameters": _object_schema(
            {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "countries": {"type": "array", "items": {"type": "string"}},
                "minimum_importance": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                },
            },
            ["start", "end", "countries", "minimum_importance"],
        ),
    },
]

TOOL_METADATA = {
    "calculate_position_size": {"mutating": False, "deterministic": True},
    "calculate_broker_position_size": {"mutating": False, "deterministic": True},
    "list_trade_plans": {"mutating": False, "deterministic": False},
    "get_trade_plan": {"mutating": False, "deterministic": False},
    "create_trade_plan": {"mutating": True, "deterministic": False},
    "add_trade_reflection": {"mutating": True, "deterministic": False},
    "analyze_chart": {"mutating": True, "deterministic": False},
    "get_system_health": {"mutating": False, "deterministic": False},
    "get_live_quote": {"mutating": False, "deterministic": False},
    "get_recent_candles": {"mutating": False, "deterministic": False},
    "get_broker_state": {"mutating": False, "deterministic": False},
    "get_market_news": {"mutating": False, "deterministic": False},
    "get_economic_calendar": {"mutating": False, "deterministic": False},
}


def _json(value: Any) -> str:
    return json.dumps(jsonable_encoder(value))


class TradingAgent:
    def __init__(
        self,
        settings: Settings,
        db: Session,
        engine: Engine,
        confirm_mutation: ConfirmMutation,
        provider: ModelProvider | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.engine = engine
        self.confirm_mutation = confirm_mutation
        self.provider = provider or create_model_provider(settings)
        self.policy = policy or PolicyEngine.load()
        self.policy.validate_tool_surface(TOOLS, TOOL_METADATA)
        self.hooks = ExecutionHooks(self.policy, confirm_mutation)
        self.last_route: ModelRoute | None = None

    def respond(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
        mode: AgentMode | None = None,
    ) -> str:
        instructions = f"{AGENT_INSTRUCTIONS}\n\n{self.policy.instructions}"
        execute_tool = policy_wrapped_executor(
            self._execute_tool,
            self.hooks,
            TOOL_METADATA,
        )
        self.last_route = route_model(
            self.settings,
            self.provider.name,
            message,
            mode=mode,
            fallback_model=self.provider.model,
        )
        return self.provider.complete(
            instructions=instructions,
            message=message,
            history=history or [],
            tools=TOOLS,
            execute_tool=execute_tool,
            max_tool_rounds=self.policy.policy.tool_policy.max_tool_rounds,
            model=self.last_route.model,
            reasoning_effort=self.last_route.reasoning_effort,
        )

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "calculate_position_size":
            result = calculate_position_size(PositionSizeRequest.model_validate(arguments))
            return _json({"ok": True, "result": result})

        if name == "calculate_broker_position_size":
            request_values = {
                key: value
                for key, value in arguments.items()
                if key not in {"provider", "symbol"}
            }
            request_values["maximum_risk_percent"] = str(
                self.settings.maximum_trade_risk_percent
            )
            request = BrokerPositionSizeRequest.model_validate(request_values)
            specification = active_instrument_specification(
                self.db,
                provider=arguments["provider"],
                external_symbol=arguments["symbol"],
            )
            result = calculate_broker_position_size(request, specification)
            return _json({"ok": True, "result": result})

        if name == "list_trade_plans":
            trades = list_trade_plans(self.db, limit=arguments["limit"])
            return _json(
                {
                    "ok": True,
                    "result": [TradePlanRead.model_validate(trade) for trade in trades],
                }
            )

        if name == "get_trade_plan":
            trade = get_trade_plan(self.db, uuid.UUID(arguments["trade_id"]))
            return _json({"ok": True, "result": TradePlanRead.model_validate(trade)})

        if name == "create_trade_plan":
            request = TradePlanCreate.model_validate(arguments)
            trade = create_trade_plan(
                self.db,
                request,
                policy_hash=self.policy.content_hash,
                source="agent",
                maximum_risk_percent=Decimal(
                    str(self.settings.maximum_trade_risk_percent)
                ),
            )
            return _json({"ok": True, "result": TradePlanRead.model_validate(trade)})

        if name == "add_trade_reflection":
            trade_id = uuid.UUID(arguments["trade_id"])
            reflection_data = {
                key: value for key, value in arguments.items() if key != "trade_id"
            }
            request = ReflectionCreate.model_validate(reflection_data)
            reflection = create_reflection(self.db, trade_id, request)
            return _json({"ok": True, "result": ReflectionRead.model_validate(reflection)})

        if name == "analyze_chart":
            path = Path(arguments["image_path"]).expanduser().resolve()
            content_type, _ = mimetypes.guess_type(path)
            if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise ValueError("chart must be PNG, JPEG, or WebP")
            image_bytes = path.read_bytes()
            if len(image_bytes) > 10 * 1024 * 1024:
                raise ValueError("image exceeds 10 MB")
            result = analyze_chart(
                image_bytes=image_bytes,
                content_type=content_type,
                user_context=arguments["context"],
                settings=self.settings,
                provider=self.provider,
                model=self.last_route.model if self.last_route else None,
                reasoning_effort=(
                    self.last_route.reasoning_effort if self.last_route else "medium"
                ),
            )
            record_chart_analysis(
                self.db,
                image_bytes=image_bytes,
                content_type=content_type,
                evidence_directory=self.settings.evidence_directory,
                analysis=result,
                provider=self.provider,
                model=self.last_route.model if self.last_route else None,
                policy_hash=self.policy.content_hash,
                prompt=SYSTEM_PROMPT,
                source="agent",
                market_time=None,
                instrument=None,
                venue=None,
                timeframe=None,
            )
            return _json({"ok": True, "result": result})

        if name == "get_system_health":
            report = check_health(self.settings, self.engine, policy=self.policy)
            return _json({"ok": True, "result": report.model_dump()})

        if name == "get_live_quote":
            async def read_quote():
                connector = create_oanda_connector(self.settings)
                try:
                    return await connector.latest_quote(arguments["instrument"])
                finally:
                    await connector.aclose()

            return _json({"ok": True, "result": asyncio.run(read_quote())})

        if name == "get_recent_candles":
            async def read_candles():
                connector = create_oanda_connector(self.settings)
                try:
                    return await connector.candles(
                        arguments["instrument"],
                        arguments["timeframe"],
                        count=arguments["count"],
                    )
                finally:
                    await connector.aclose()

            return _json({"ok": True, "result": asyncio.run(read_candles())})

        if name == "get_broker_state":
            async def read_broker_state():
                connector = create_oanda_connector(self.settings)
                try:
                    account = await connector.account()
                    positions = await connector.positions()
                    return {
                        "currency": account.currency,
                        "balance": account.balance,
                        "equity": account.equity,
                        "margin_used": account.margin_used,
                        "margin_available": account.margin_available,
                        "as_of": account.retrieved_at,
                        "source": account.source,
                        "positions": positions,
                    }
                finally:
                    await connector.aclose()

            return _json({"ok": True, "result": asyncio.run(read_broker_state())})

        if name == "get_market_news":
            async def read_news():
                connector = create_news_connector(self.settings)
                try:
                    return await connector.news(
                        country=arguments["country"],
                        limit=arguments["limit"],
                    )
                finally:
                    await connector.aclose()

            return _json({"ok": True, "result": asyncio.run(read_news())})

        if name == "get_economic_calendar":
            async def read_calendar():
                connector = create_news_connector(self.settings)
                try:
                    return await connector.calendar(
                        start=date.fromisoformat(arguments["start"]),
                        end=date.fromisoformat(arguments["end"]),
                        countries=arguments["countries"],
                        minimum_importance=arguments["minimum_importance"],
                    )
                finally:
                    await connector.aclose()

            return _json({"ok": True, "result": asyncio.run(read_calendar())})

        raise ValueError(f"unknown tool: {name}")
