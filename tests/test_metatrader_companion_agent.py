import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.policy import PolicyViolation, policy_wrapped_executor
from app.services.agent import TradingAgent
from app.services.agent_tools import TOOL_METADATA
from tests.test_metatrader_companion_api import setup


def test_agent_existing_broker_tool_receives_evidence_without_account_id(monkeypatch):
    _, connector, _ = setup()
    agent = TradingAgent(
        db=Mock(),
        engine=Mock(),
        settings=Settings(),
        confirm_mutation=lambda *_: False,
        provider=SimpleNamespace(name="test", model="test"),
    )
    monkeypatch.setattr(agent, "_broker_connector", lambda: connector)
    execute = policy_wrapped_executor(agent._execute_tool, agent.hooks, TOOL_METADATA)
    output = json.loads(execute("get_broker_state", {}))
    assert output["ok"] is True
    serialized = json.dumps(output)
    assert "companion_evidence" in serialized
    assert "recent_window" in serialized
    assert '"account_id"' not in serialized
    agent.db.commit.assert_not_called()

    blocked = Mock(side_effect=PolicyViolation("blocked by policy"))
    monkeypatch.setattr(agent.hooks, "before_execute", blocked)
    provider_read = Mock()
    monkeypatch.setattr(agent, "_broker_connector", provider_read)
    with pytest.raises(PolicyViolation):
        execute("get_broker_state", {})
    provider_read.assert_not_called()
