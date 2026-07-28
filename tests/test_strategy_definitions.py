import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.services.catalog import verify_playbook_version_integrity
from app.services.strategy_definitions import (
    canonical_strategy_definition,
    strategy_proposal_hash,
)

MAXIMUM_RISK = Decimal("1")


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
