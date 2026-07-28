from pathlib import Path

import pytest

from app.harness_context import HARNESS_ROOT, select_harness_context


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Review this chart screenshot.", "skills/chart-analysis/SKILL.md"),
        ("Build my premarket plan for New York.", "skills/premarket-planning/SKILL.md"),
        ("Size this position from my entry and stop.", "skills/position-planning/SKILL.md"),
        ("Review my closed trade and partial exit.", "skills/trade-review/SKILL.md"),
        ("Analyze expectancy and whether I have an edge.", "skills/edge-analysis/SKILL.md"),
        ("Is this Wyckoff reaccumulation?", "market-models/wyckoff.md"),
        (
            "Did price sweep equal highs and mitigate the FVG?",
            "market-models/liquidity-imbalance.md",
        ),
        ("The market shifted; should I use a lower timeframe?", "market-models/market-regimes.md"),
        ("I hesitated after a losing streak. Help with mindset.", "psychology/PSYCHOLOGY.md"),
        ("Apply Mark Douglas probabilistic thinking.", "psychology/probabilistic-execution.md"),
        ("Review this XAUUSD setup.", "references/xauusd.md"),
        ("Check the London session context.", "references/REFERENCES.md"),
        ("Teach me the next lesson in my curriculum.", "learning/LEARNING.md"),
    ],
)
def test_harness_routes_representative_trading_prompts(
    prompt: str,
    expected: str,
) -> None:
    context = select_harness_context(prompt)

    assert context.paths[0] == "HARNESS.md"
    assert expected in context.paths
    assert len(context.paths) <= 5


def test_harness_loads_only_entrypoint_for_unrelated_request() -> None:
    context = select_harness_context("Hello there.")

    assert context.paths == ("HARNESS.md",)


def test_active_strategy_can_exclude_generic_market_methodologies() -> None:
    context = select_harness_context(
        "Compare a Wyckoff spring with an ICT fair value gap.",
        excluded_prefixes=("market-models/",),
    )

    assert all(not path.startswith("market-models/") for path in context.paths)


def test_required_lesson_source_is_loaded_without_trigger_match() -> None:
    context = select_harness_context(
        "Teach me lesson-market-mechanics.",
        required_paths=("references/REFERENCES.md",),
    )

    assert "references/REFERENCES.md" in context.paths


def test_required_lesson_source_cannot_escape_harness_root() -> None:
    with pytest.raises(ValueError, match="invalid required harness resource"):
        select_harness_context(
            "Teach me.",
            required_paths=("../private.md",),
        )


def test_harness_rejects_oversized_resource(tmp_path: Path) -> None:
    (tmp_path / "HARNESS.md").write_text(
        "---\nname: Test\ndescription: Test\n---\nRoot",
        encoding="utf-8",
    )
    (tmp_path / "large.md").write_text(
        "---\ndescription: Large\ntriggers: chart\n---\n" + ("x" * 33_000),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="too large"):
        select_harness_context("chart", root=tmp_path)


def test_harness_structure_has_valid_entrypoint_and_skill_names() -> None:
    entrypoint = (HARNESS_ROOT / "HARNESS.md").read_text(encoding="utf-8")

    assert "name: Trading Decision Support" in entrypoint
    assert "description:" in entrypoint
    assert len(entrypoint.split()) < 300
    assert (HARNESS_ROOT / ".leaf-detectors").read_text(encoding="utf-8").strip() == (
        "skill=SKILL.md"
    )
    for skill in HARNESS_ROOT.rglob("SKILL.md"):
        content = skill.read_text(encoding="utf-8")
        assert f"name: {skill.parent.name}" in content
        assert "description:" in content
        assert "triggers:" in content
