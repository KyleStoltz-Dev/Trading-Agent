import json
import mimetypes
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.policy import ExecutionHooks, PolicyEngine, policy_wrapped_executor
from app.providers import ModelProvider, create_model_provider
from app.schemas import (
    PositionSizeRequest,
    ReflectionCreate,
    ReflectionRead,
    TradePlanCreate,
    TradePlanRead,
)
from app.services.chart_analysis import analyze_chart
from app.services.health import check_health
from app.services.journal import (
    create_reflection,
    create_trade_plan,
    get_trade_plan,
    list_trade_plans,
)
from app.services.risk import calculate_position_size

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
            },
            [
                "instrument",
                "venue",
                "direction",
                "setup_name",
                "regime",
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
]

TOOL_METADATA = {
    "calculate_position_size": {"mutating": False, "deterministic": True},
    "list_trade_plans": {"mutating": False, "deterministic": False},
    "get_trade_plan": {"mutating": False, "deterministic": False},
    "create_trade_plan": {"mutating": True, "deterministic": False},
    "add_trade_reflection": {"mutating": True, "deterministic": False},
    "analyze_chart": {"mutating": False, "deterministic": False},
    "get_system_health": {"mutating": False, "deterministic": False},
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

    def respond(self, message: str, history: list[dict[str, str]] | None = None) -> str:
        instructions = f"{AGENT_INSTRUCTIONS}\n\n{self.policy.instructions}"
        execute_tool = policy_wrapped_executor(
            self._execute_tool,
            self.hooks,
            TOOL_METADATA,
        )
        return self.provider.complete(
            instructions=instructions,
            message=message,
            history=history or [],
            tools=TOOLS,
            execute_tool=execute_tool,
            max_tool_rounds=self.policy.policy.tool_policy.max_tool_rounds,
        )

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "calculate_position_size":
            result = calculate_position_size(PositionSizeRequest.model_validate(arguments))
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
            trade = create_trade_plan(self.db, request)
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
            )
            return _json({"ok": True, "result": result})

        if name == "get_system_health":
            report = check_health(self.settings, self.engine, policy=self.policy)
            return _json({"ok": True, "result": report.model_dump()})

        raise ValueError(f"unknown tool: {name}")
