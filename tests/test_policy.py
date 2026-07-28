from pathlib import Path

import pytest

from app.policy import PolicyEngine, PolicyViolation, ToolContext


def test_policy_is_loaded_with_hard_order_boundary() -> None:
    policy = PolicyEngine.load()

    assert policy.version == "1.7.0"
    assert "place_order" in policy.policy.tool_policy.forbidden_names
    assert "human_controls_orders" in policy.instructions


def test_policy_change_after_startup_fails_closed(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "app" / "trading-rules.json"
    policy_path = tmp_path / "trading-rules.json"
    policy_path.write_bytes(source.read_bytes())
    policy = PolicyEngine.load(policy_path)
    policy_path.write_text(policy_path.read_text().replace("1.7.0", "1.7.1"))

    with pytest.raises(PolicyViolation, match="changed after startup"):
        policy.authorize(
            ToolContext(
                name="calculate_position_size",
                arguments={},
                mutating=False,
                deterministic=True,
            )
        )
