import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.config import Settings
from app.services.agent import TOOLS, TradingAgent


class FakeResponses:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.outputs)


def test_agent_executes_risk_tool_and_returns_final_text() -> None:
    tool_call = SimpleNamespace(
        type="function_call",
        name="calculate_position_size",
        call_id="call-1",
        arguments=json.dumps(
            {
                "account_equity": "10000",
                "risk_percent": "1",
                "entry": "2000",
                "stop": "1990",
                "target": "2040",
                "value_per_price_unit": "1",
            }
        ),
    )
    fake_responses = FakeResponses(
        [
            SimpleNamespace(output=[tool_call], output_text=""),
            SimpleNamespace(output=[], output_text="Risk is $100 and planned R is 4."),
        ]
    )
    client = SimpleNamespace(responses=fake_responses)
    agent = TradingAgent(
        settings=Settings(openai_api_key="test"),
        db=Mock(),
        engine=Mock(),
        confirm_mutation=Mock(return_value=False),
        client=client,
    )

    result = agent.respond("Calculate this risk.")

    assert result == "Risk is $100 and planned R is 4."
    second_input = fake_responses.calls[1]["input"]
    tool_outputs = [item for item in second_input if isinstance(item, dict)]
    function_output = next(
        item for item in tool_outputs if item.get("type") == "function_call_output"
    )
    payload = json.loads(function_output["output"])
    assert payload["ok"] is True
    assert payload["result"]["quantity"] == "10.00000000"
    assert fake_responses.calls[0]["store"] is False
    assert fake_responses.calls[0]["reasoning"]["context"] == "current_turn"
    assert fake_responses.calls[0]["safety_identifier"] == "trading-agent-local"


def test_agent_does_not_apply_declined_mutation() -> None:
    confirmation = Mock(return_value=False)
    agent = TradingAgent(
        settings=Settings(openai_api_key="test"),
        db=Mock(),
        engine=Mock(),
        confirm_mutation=confirmation,
        client=SimpleNamespace(responses=Mock()),
    )
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

    result = json.loads(agent._execute_tool("create_trade_plan", arguments))

    assert result == {"ok": False, "error": "trader declined journal mutation"}
    confirmation.assert_called_once()
    agent.db.add.assert_not_called()


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
