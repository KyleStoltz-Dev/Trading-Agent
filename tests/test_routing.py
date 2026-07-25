from app.config import Settings
from app.routing import classify_task, route_model


def test_auto_routing_uses_effort_profiles_without_inventing_model_names() -> None:
    settings = Settings(
        openai_model="base-model",
        openai_economy_model="small-model",
        openai_deep_model="large-model",
    )

    routine = route_model(settings, "openai", "Log this trade in my journal.")
    analysis = route_model(settings, "openai", "Review this chart and my planned entry.")
    deep = route_model(settings, "openai", "Deeply research my full history to find my edge.")

    assert (routine.mode, routine.model, routine.reasoning_effort) == (
        "economy",
        "small-model",
        "low",
    )
    assert (analysis.mode, analysis.model, analysis.reasoning_effort) == (
        "balanced",
        "base-model",
        "medium",
    )
    assert (deep.mode, deep.model, deep.reasoning_effort) == (
        "deep",
        "large-model",
        "high",
    )


def test_user_mode_override_wins_over_task_classification() -> None:
    route = route_model(
        Settings(anthropic_model="sonnet"),
        "anthropic",
        "Deep research across every trade.",
        mode="economy",
    )

    assert route.task_class == "deep_research"
    assert route.mode == "economy"
    assert route.model == "sonnet"
    assert route.reason == "user-selected economy mode"


def test_strategy_language_alone_is_not_software_development() -> None:
    assert classify_task("I need to change how I enter this setup") == "analysis"


def test_ollama_uses_local_model_profiles() -> None:
    settings = Settings(
        model_provider="ollama",
        ollama_model="qwen3.5:9b",
        ollama_deep_model="qwen3.5:35b-a3b",
    )

    route = route_model(settings, "ollama", "Deep research my full history.")

    assert route.model == "qwen3.5:35b-a3b"
    assert route.mode == "deep"


def test_session_model_override_keeps_mode_effort() -> None:
    route = route_model(
        Settings(model_provider="ollama"),
        "ollama",
        "Log this routine note.",
        model_override="qwen3.5:35b-a3b",
    )

    assert route.model == "qwen3.5:35b-a3b"
    assert route.mode == "economy"
    assert route.reasoning_effort == "low"
    assert "session model override" in route.reason
