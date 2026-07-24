import json
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.policy import PolicyViolation
from app.services.agent import TOOLS, TradingAgent


class RiskToolProvider:
    name = "test"
    model = "test-model"

    def __init__(self) -> None:
        self.arguments = {
            "account_equity": "10000",
            "risk_percent": "1",
            "entry": "2000",
            "stop": "1990",
            "target": "2040",
            "value_per_price_unit": "1",
        }
        self.instructions = ""

    def complete(self, *, instructions, execute_tool, **kwargs) -> str:
        self.instructions = instructions
        payload = json.loads(execute_tool("calculate_position_size", self.arguments))
        assert payload["result"]["quantity"] == "10.00000000"
        return "Risk is $100 and planned R is 4."

    def analyze_chart(self, **kwargs):
        raise AssertionError("not used")


class MutationProvider:
    name = "test"
    model = "test-model"

    def __init__(self, arguments) -> None:
        self.arguments = arguments

    def complete(self, *, execute_tool, **kwargs) -> str:
        execute_tool("create_trade_plan", self.arguments)
        return "unreachable"

    def analyze_chart(self, **kwargs):
        raise AssertionError("not used")


def test_agent_executes_risk_tool_and_loads_runtime_policy() -> None:
    provider = RiskToolProvider()
    agent = TradingAgent(
        settings=Settings(),
        db=Mock(),
        engine=Mock(),
        confirm_mutation=Mock(return_value=False),
        provider=provider,
    )

    result = agent.respond("Calculate this risk.")

    assert result == "Risk is $100 and planned R is 4."
    assert "Runtime policy 1.0.0" in provider.instructions
    assert "human_controls_orders" in provider.instructions


def test_agent_does_not_apply_declined_mutation() -> None:
    confirmation = Mock(return_value=False)
    arguments = {
        "instrument": "XAUUSD",
        "venue": "OANDA",
        "direction": "short",
        "setup_name": "liquidity sweep",
        "regime": "range",
        "context_timeframe": "4h",
        "trigger_timeframe": "5m",
        "entry": "2000",
        "stop": "2010",
        "target": "1960",
        "account_equity": "10000",
        "risk_percent": "1",
        "value_per_price_unit": "1",
        "thesis": "Sweep and rejection from external liquidity.",
        "invalidation": "Acceptance above the swept high.",
        "observations": ["Price traded above the reference high."],
        "interpretations": ["The move may be a liquidity sweep."],
    }
    db = Mock()
    agent = TradingAgent(
        settings=Settings(),
        db=db,
        engine=Mock(),
        confirm_mutation=confirmation,
        provider=MutationProvider(arguments),
    )

    with pytest.raises(PolicyViolation, match="declined"):
        agent.respond("Journal this trade.")

    confirmation.assert_called_once()
    db.add.assert_not_called()


def test_all_function_schemas_are_strict() -> None:
    def assert_strict_objects(schema: dict) -> None:
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for child in schema["properties"].values():
                assert_strict_objects(child)
        if schema.get("type") == "array":
            assert_strict_objects(schema["items"])

    for tool in TOOLS:
        assert tool["strict"] is True
        assert_strict_objects(tool["parameters"])
