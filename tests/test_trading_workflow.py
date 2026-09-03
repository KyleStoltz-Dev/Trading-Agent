from app.services.trading_workflow import infer_workflow_checkpoint


def test_workflow_checkpoint_resumes_latest_trading_goal() -> None:
    checkpoint = infer_workflow_checkpoint(
        [
            "Prepare my New York session for XAUUSD.",
            "Now review my recent trades and holding-time patterns.",
            "continue",
        ]
    )

    assert checkpoint is not None
    assert checkpoint.stage == "review"
    assert checkpoint.instrument == "XAU_USD"
    assert checkpoint.label == "Review recent trades"


def test_workflow_checkpoint_normalizes_instrument() -> None:
    checkpoint = infer_workflow_checkpoint(
        ["Help me analyze EUR/USD market context before New York."]
    )

    assert checkpoint is not None
    assert checkpoint.stage == "analyze"
    assert checkpoint.instrument == "EUR_USD"


def test_reviewing_a_chart_is_market_analysis_not_trade_history() -> None:
    checkpoint = infer_workflow_checkpoint(["Review my XAUUSD chart."])

    assert checkpoint is not None
    assert checkpoint.stage == "analyze"


def test_workflow_checkpoint_is_not_created_for_unrelated_chat() -> None:
    assert infer_workflow_checkpoint(["How do models work?"]) is None


def test_workflow_prompt_prioritizes_available_context_over_questions() -> None:
    checkpoint = infer_workflow_checkpoint(["Review my recent trades."])

    assert checkpoint is not None
    context = checkpoint.prompt_context()
    assert "Gather available broker, market, news, strategy, and journal facts" in context
    assert "Ask only for a missing human judgment" in context
