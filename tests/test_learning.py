import uuid

import pytest

from app.models import TradingAccount
from app.schemas import TraderProfileUpsert
from app.services.learning import (
    add_custom_learning_module,
    configure_learning_curriculum,
    curriculum_for_profile,
    curriculum_modules,
    curriculum_read,
    default_teaching_mode,
    is_learning_request,
    module_read,
    update_learning_module,
)
from app.services.strategy_workspace import upsert_trader_profile
from app.services.workspaces import RequestScope


def _profile(db_session, request_scope, *, level: str = "beginner"):
    return upsert_trader_profile(
        db_session,
        TraderProfileUpsert(
            display_name="Learning Trader",
            timezone="America/New_York",
            experience_level=level,
            trading_style="Discretionary",
            markets=["XAUUSD"],
            sessions=["New York"],
            goals=["consistency"],
            risk_preferences={"maximum_trade_risk_percent": 1.0},
        ),
        scope=request_scope,
    )


def test_experience_levels_choose_different_teaching_defaults() -> None:
    assert default_teaching_mode("beginner") == "guided"
    assert default_teaching_mode("intermediate") == "flexible"
    assert default_teaching_mode("advanced") == "on_demand"
    assert is_learning_request("Teach me the next lesson.")
    assert is_learning_request("What is Wyckoff accumulation?")
    assert is_learning_request("Switch me to guided mode.")
    assert is_learning_request("What is an FVG?")
    assert is_learning_request("Walk me through a spring.")
    assert is_learning_request("Why does CPI move gold?")
    assert is_learning_request("Tell me about RSI.")
    assert not is_learning_request("Should I take this ICT trade right now?")


def test_curriculum_is_durable_ordered_and_source_tiered(
    db_session,
    request_scope,
) -> None:
    profile = _profile(db_session, request_scope)

    curriculum = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="beginner",
        teaching_mode="guided",
        selected_topics=["foundations", "news-macro", "wyckoff"],
    )

    assert curriculum is not None
    result = curriculum_read(db_session, curriculum, scope=request_scope)
    assert result["teaching_mode"] == "guided"
    assert [item["key"] for item in result["modules"]] == [
        "probability-and-process",
        "news-and-macro",
        "wyckoff-framework",
    ]
    assert result["next_module"]["key"] == "probability-and-process"
    assert result["source_tier_policy"]["tier_1"].startswith("Use the local")
    assert result["source_tier_policy"]["strategy_boundary"].startswith(
        "Education about a framework"
    )
    news = result["modules"][1]
    assert "federalreserve.gov" in news["source_plan"]["preferred_domains"]


def test_progress_and_completed_lessons_survive_topic_reconfiguration(
    db_session,
    request_scope,
) -> None:
    profile = _profile(db_session, request_scope, level="advanced")
    curriculum = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="advanced",
        teaching_mode="on_demand",
        selected_topics=["wyckoff", "ict-smc"],
    )
    assert curriculum is not None

    completed = update_learning_module(
        db_session,
        curriculum,
        "wyckoff-framework",
        scope=request_scope,
        status="completed",
        learner_notes="I can distinguish a spring hypothesis from confirmation.",
        evidence_references=[
            {
                "kind": "harness",
                "label": "Wyckoff",
                "locator": "market-models/wyckoff.md",
                "retrieved_at": None,
            }
        ],
    )
    assert completed.completed_at is not None

    updated = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="advanced",
        teaching_mode="flexible",
        selected_topics=["ict-smc"],
    )
    assert updated is not None
    visible = curriculum_modules(db_session, updated.id, scope=request_scope)
    all_modules = curriculum_modules(
        db_session,
        updated.id,
        scope=request_scope,
        included_only=False,
    )

    assert [module.module_key for module in visible] == ["ict-smc-framework"]
    preserved = next(
        module for module in all_modules if module.module_key == "wyckoff-framework"
    )
    assert preserved.status == "completed"
    assert preserved.included is False
    assert preserved.learner_notes.startswith("I can distinguish")


def test_pausing_curriculum_preserves_progress(
    db_session,
    request_scope,
) -> None:
    profile = _profile(db_session, request_scope)
    curriculum = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="beginner",
        teaching_mode="guided",
        selected_topics=["foundations"],
    )
    assert curriculum is not None

    paused = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="beginner",
        teaching_mode=None,
        selected_topics=[],
    )

    assert paused is not None
    assert paused.status == "paused"
    assert len(curriculum_modules(db_session, paused.id, scope=request_scope)) == 1


def test_custom_lesson_is_bounded_idempotent_and_appended(
    db_session,
    request_scope,
) -> None:
    profile = _profile(db_session, request_scope, level="intermediate")
    curriculum = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="intermediate",
        teaching_mode="flexible",
        selected_topics=["foundations"],
    )
    assert curriculum is not None

    lesson = add_custom_learning_module(
        db_session,
        curriculum,
        scope=request_scope,
        title="Treasury yields and gold",
        category="News Macro",
        framework=None,
        objectives=[
            "Separate an observed yield change from a directional gold claim."
        ],
        source_queries=["Treasury yields relationship to gold education"],
        preferred_domains=["federalreserve.gov"],
    )
    same_lesson = add_custom_learning_module(
        db_session,
        curriculum,
        scope=request_scope,
        title="Treasury yields and gold",
        category="news-macro",
        framework=None,
        objectives=["A repeated request must not create a duplicate."],
        source_queries=["Treasury yields and gold"],
        preferred_domains=[],
    )

    assert lesson.id == same_lesson.id
    assert lesson.module_key == "custom-treasury-yields-and-gold"
    assert lesson.sequence == 2
    assert lesson.source_plan == {
        "local": [],
        "queries": ["Treasury yields relationship to gold education"],
        "preferred_domains": ["federalreserve.gov"],
    }
    assert len(
        curriculum_modules(db_session, curriculum.id, scope=request_scope)
    ) == 2

    reconfigured = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="intermediate",
        teaching_mode="flexible",
        selected_topics=["risk"],
    )
    assert reconfigured is not None
    assert [
        module.module_key
        for module in curriculum_modules(
            db_session,
            reconfigured.id,
            scope=request_scope,
        )
    ] == ["risk-and-r-multiples", "custom-treasury-yields-and-gold"]

    expanded = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="intermediate",
        teaching_mode="flexible",
        selected_topics=["foundations", "risk", "news-macro"],
    )
    assert expanded is not None
    expanded_modules = curriculum_modules(
        db_session,
        expanded.id,
        scope=request_scope,
    )
    assert [module.sequence for module in expanded_modules] == [1, 2, 3, 4]
    assert expanded_modules[-1].module_key == "custom-treasury-yields-and-gold"


def test_started_lesson_is_next_even_when_an_earlier_lesson_is_available(
    db_session,
    request_scope,
) -> None:
    profile = _profile(db_session, request_scope, level="advanced")
    curriculum = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="advanced",
        teaching_mode="on_demand",
        selected_topics=["foundations", "risk"],
    )
    assert curriculum is not None
    update_learning_module(
        db_session,
        curriculum,
        "risk-and-r-multiples",
        scope=request_scope,
        status="in_progress",
    )

    assert curriculum_read(
        db_session,
        curriculum,
        scope=request_scope,
    )["next_module"]["key"] == "risk-and-r-multiples"


def test_learning_records_reject_cross_account_reads_and_mutations(
    db_session,
    workspace_account,
    request_scope,
) -> None:
    workspace, _ = workspace_account
    profile = _profile(db_session, request_scope)
    curriculum = configure_learning_curriculum(
        db_session,
        profile,
        scope=request_scope,
        experience_level="beginner",
        teaching_mode="guided",
        selected_topics=["foundations"],
    )
    assert curriculum is not None
    module = curriculum_modules(
        db_session,
        curriculum.id,
        scope=request_scope,
    )[0]

    other_account = TradingAccount(
        workspace_id=workspace.id,
        broker="manual",
        external_account_id=f"other-{uuid.uuid4().hex}",
        label="Other account",
        currency="USD",
        mode="practice",
        is_default=False,
    )
    db_session.add(other_account)
    db_session.commit()
    other_scope = RequestScope(
        workspace_id=workspace.id,
        account_id=other_account.id,
    )

    assert (
        curriculum_for_profile(
            db_session,
            profile.id,
            scope=other_scope,
        )
        is None
    )
    with pytest.raises(LookupError, match="requested scope"):
        curriculum_modules(
            db_session,
            curriculum.id,
            scope=other_scope,
        )
    with pytest.raises(LookupError, match="requested scope"):
        curriculum_read(
            db_session,
            curriculum,
            scope=other_scope,
        )
    with pytest.raises(LookupError, match="requested scope"):
        module_read(
            db_session,
            module,
            scope=other_scope,
        )
    with pytest.raises(LookupError, match="requested scope"):
        configure_learning_curriculum(
            db_session,
            profile,
            scope=other_scope,
            experience_level="beginner",
            teaching_mode="guided",
            selected_topics=["risk"],
        )
    with pytest.raises(LookupError, match="requested scope"):
        update_learning_module(
            db_session,
            curriculum,
            module.module_key,
            scope=other_scope,
            status="completed",
        )
    with pytest.raises(LookupError, match="requested scope"):
        add_custom_learning_module(
            db_session,
            curriculum,
            scope=other_scope,
            title="Cross-account lesson",
            category="testing",
            framework=None,
            objectives=["This must never be written to the other account."],
            source_queries=["cross account isolation"],
            preferred_domains=[],
        )

    db_session.refresh(module)
    assert module.status == "available"
