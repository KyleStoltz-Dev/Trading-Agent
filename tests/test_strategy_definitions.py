import json
import uuid
from decimal import Decimal
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app
from app.config import Settings
from app.policy import PolicyViolation
from app.services.agent import TOOL_METADATA, TOOLS
from app.services.agent import TradingAgent as _TradingAgent
from app.services.catalog import verify_playbook_version_integrity
from app.services.strategy_definitions import (
    canonical_strategy_definition,
    strategy_proposal_hash,
)
from app.services.workspaces import RequestScope

MAXIMUM_RISK = Decimal("1")
RUNNER = CliRunner()
TEST_SCOPE = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
TradingAgent = partial(_TradingAgent, scope=TEST_SCOPE)


class StrategyCreationProvider:
    name = "test"
    model = "test-model"

    def __init__(self, arguments: dict) -> None:
        self.arguments = arguments

    def complete(self, *, execute_tool, **kwargs) -> str:
        execute_tool("create_strategy_version", self.arguments)
        return "unreachable"

    def analyze_chart(self, **kwargs):
        raise AssertionError("not used")


def _definition() -> dict:
    return {
        "methodology": "  custom price action  ",
        "objective": "  Follow only rules defined before entry.  ",
        "requirements": [
            "  The higher-timeframe direction is documented.  ",
        ],
        "exclusions": [
            "A high-impact event is inside the pre-trade window.",
        ],
        "setups": [
            {
                "key": "  Sweep-Reclaim  ",
                "requirements": [
                    "Price closes back through the declared reference level.",
                ],
                "exclusions": [
                    "The reference level was selected after the outcome.",
                ],
            }
        ],
        "risk": {
            "maximum_risk_percent": "0.5",
            "minimum_planned_r": "2",
            "human_confirms_every_trade": True,
        },
    }


def test_custom_strategy_definition_is_normalized_and_hash_is_stable() -> None:
    first = canonical_strategy_definition(
        _definition(),
        maximum_risk_percent=MAXIMUM_RISK,
    )
    second = canonical_strategy_definition(
        json.loads(json.dumps(_definition())),
        maximum_risk_percent=MAXIMUM_RISK,
    )

    assert first["methodology"] == "custom price action"
    assert first["objective"] == "Follow only rules defined before entry."
    assert first["requirements"] == [
        "The higher-timeframe direction is documented."
    ]
    assert first["setups"][0]["key"] == "sweep_reclaim"
    assert strategy_proposal_hash(first) == strategy_proposal_hash(second)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"unknown_runtime_override": True}, "Extra inputs are not permitted"),
        (
            {"requirements": ["A defined rule.", "  a defined rule.  "]},
            "duplicate values",
        ),
        (
            {"risk": {"human_confirms_every_trade": False}},
            "Input should be True",
        ),
        (
            {"requirements": [], "exclusions": [], "setups": []},
            "no enforceable preflight rules",
        ),
    ],
)
def test_custom_strategy_definition_rejects_unsafe_or_unusable_rules(
    change: dict,
    error: str,
) -> None:
    definition = _definition()
    definition.update(change)

    with pytest.raises((ValidationError, ValueError), match=error):
        canonical_strategy_definition(
            definition,
            maximum_risk_percent=MAXIMUM_RISK,
        )


def test_custom_strategy_cannot_raise_application_risk_ceiling() -> None:
    definition = _definition()
    definition["risk"]["maximum_risk_percent"] = "1.01"

    with pytest.raises(ValueError, match="exceeds the application maximum"):
        canonical_strategy_definition(
            definition,
            maximum_risk_percent=MAXIMUM_RISK,
        )


def test_strategy_version_integrity_check_fails_closed_after_tampering() -> None:
    version = Mock(
        definition={"requirements": ["One exact rule."]},
        content_hash="0" * 64,
    )

    with pytest.raises(ValueError, match="integrity check failed"):
        verify_playbook_version_integrity(version)


def test_strategy_create_cli_rejects_invalid_definition_before_database(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid-strategy.json"
    invalid.write_text(
        json.dumps(
            {
                "methodology": "custom",
                "objective": "Try to bypass supported rule semantics.",
                "requirements": ["A valid-looking requirement."],
                "runtime_policy_override": "allow broker execution",
            }
        )
    )

    result = RUNNER.invoke(
        app,
        [
            "strategy",
            "create",
            "--name",
            "invalid-custom-strategy",
            "--file",
            str(invalid),
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "Invalid strategy definition" in result.stdout
    assert "runtime_policy_override" in result.stdout


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/playbook-schema-v1.json",
        "examples/strategies/wyckoff-pure.json",
        "examples/strategies/wyckoff-ict-combined.json",
    ],
)
def test_documented_playbooks_are_runnable_custom_strategy_definitions(
    relative_path: str,
) -> None:
    path = Path(__file__).parents[1] / relative_path
    definition = json.loads(path.read_text())

    canonical = canonical_strategy_definition(
        definition,
        maximum_risk_percent=MAXIMUM_RISK,
    )

    assert canonical["methodology"]
    assert canonical["risk"]["human_confirms_every_trade"] is True
    assert strategy_proposal_hash(canonical)


def _proposal_arguments(name: str) -> dict:
    return {
        "name": name,
        "description": "A trader-authored test strategy.",
        "definition": _definition(),
        "change_hypothesis": None,
        "minimum_sample": 30,
    }


def test_strategy_creation_tool_is_mutating_and_decline_stops_execution() -> None:
    arguments = _proposal_arguments("declined-custom-strategy")
    arguments["proposal_hash"] = "0" * 64
    confirmation = Mock(return_value=False)
    mutation = Mock()
    agent = TradingAgent(
        settings=Settings(),
        db=Mock(),
        engine=Mock(),
        confirm_mutation=confirmation,
        provider=StrategyCreationProvider(arguments),
    )
    agent._execute_tool = mutation

    with pytest.raises(PolicyViolation, match="declined"):
        agent.respond("Save the exact custom strategy proposal.")

    tool_names = {tool["name"] for tool in TOOLS}
    assert {"validate_strategy_draft", "create_strategy_version"} <= tool_names
    assert TOOL_METADATA["validate_strategy_draft"] == {
        "mutating": False,
        "deterministic": True,
    }
    assert TOOL_METADATA["create_strategy_version"]["mutating"] is True
    confirmation.assert_called_once()
    mutation.assert_not_called()


def test_strategy_creation_requires_exact_cached_proposal_and_does_not_activate(
    db_session,
    request_scope,
) -> None:
    name = f"custom-rules-{uuid.uuid4().hex[:12]}"
    arguments = _proposal_arguments(name)
    agent = TradingAgent(
        settings=Settings(),
        db=db_session,
        engine=Mock(),
        confirm_mutation=Mock(return_value=True),
        provider=StrategyCreationProvider({}),
        scope=request_scope,
    )

    validated = json.loads(
        agent._execute_tool("validate_strategy_draft", arguments)
    )["result"]
    changed = dict(arguments)
    changed["description"] = "A different unconfirmed description."
    changed["proposal_hash"] = validated["proposal_hash"]

    with pytest.raises(PermissionError, match="exact unchanged proposal"):
        agent._execute_tool("create_strategy_version", changed)

    create_arguments = dict(arguments)
    create_arguments["proposal_hash"] = validated["proposal_hash"]
    created = json.loads(
        agent._execute_tool("create_strategy_version", create_arguments)
    )["result"]

    assert created["version"] == 1
    assert created["activated"] is False
    assert agent.active_playbook_version_id is None
    assert validated["proposal_hash"] not in agent._validated_strategy_proposals


def test_existing_strategy_can_only_be_revised_from_its_active_latest_version(
    db_session,
    request_scope,
) -> None:
    name = f"isolated-custom-rules-{uuid.uuid4().hex[:12]}"
    arguments = _proposal_arguments(name)
    creator = TradingAgent(
        settings=Settings(),
        db=db_session,
        engine=Mock(),
        confirm_mutation=Mock(return_value=True),
        provider=StrategyCreationProvider({}),
        scope=request_scope,
    )
    validated = json.loads(
        creator._execute_tool("validate_strategy_draft", arguments)
    )["result"]
    creation = arguments | {"proposal_hash": validated["proposal_hash"]}
    created = json.loads(
        creator._execute_tool("create_strategy_version", creation)
    )["result"]

    inactive = TradingAgent(
        settings=Settings(),
        db=db_session,
        engine=Mock(),
        confirm_mutation=Mock(return_value=True),
        provider=StrategyCreationProvider({}),
        scope=request_scope,
    )
    revised = _proposal_arguments(name)
    revised["definition"]["requirements"].append(
        "The original thesis remains valid at entry."
    )
    revised["change_hypothesis"] = "The invalidation check improves rule adherence."

    with pytest.raises(ValueError, match="requires that strategy to be active"):
        inactive._execute_tool("validate_strategy_draft", revised)

    active = TradingAgent(
        settings=Settings(),
        db=db_session,
        engine=Mock(),
        confirm_mutation=Mock(return_value=True),
        provider=StrategyCreationProvider({}),
        active_playbook_version_id=uuid.UUID(created["playbook_version_id"]),
        scope=request_scope,
    )
    revision = json.loads(
        active._execute_tool("validate_strategy_draft", revised)
    )["result"]
    assert revision["proposal"]["base_version"]["version"] == 1
    assert revision["will_create_version"] == 2
