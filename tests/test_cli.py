import uuid
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import typer
from rich.console import Console
from typer.testing import CliRunner

import app.cli as cli_module
from app.cli import app
from app.costs import TokenUsage
from app.schemas import MindsetCheckInRead
from app.services.agent import UsedReference
from app.services.health import HealthCheck, HealthReport

runner = CliRunner()


def test_help_lists_interactive_and_fallback_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "chat",
        "setup",
        "health",
        "risk",
        "preflight",
        "plan",
        "review",
        "chart",
        "api",
        "journal",
        "sessions",
        "develop",
        "mindset",
    ):
        assert command in result.stdout


def test_risk_command_uses_deterministic_calculator() -> None:
    result = runner.invoke(
        app,
        [
            "risk",
            "--account-equity",
            "10000",
            "--risk-percent",
            "1",
            "--entry",
            "2000",
            "--stop",
            "1990",
            "--target",
            "2040",
            "--value-per-price-unit",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert '"risk_amount": "100.00"' in result.stdout
    assert '"quantity": "10.00000000"' in result.stdout
    assert '"planned_r": "4.0000"' in result.stdout


def test_preflight_help_states_strategy_grade_and_no_order_boundary() -> None:
    result = runner.invoke(app, ["preflight", "--help"])

    assert result.exit_code == 0
    assert "exact strategy" in result.stdout
    assert "never places an" in result.stdout


def test_chat_trade_intent_offers_existing_preflight_with_default_yes(
    monkeypatch,
) -> None:
    version_id = uuid.uuid4()
    conversation = SimpleNamespace(
        name="gold-entry",
        active_playbook_version_id=version_id,
    )
    add_turn = Mock()
    launch = Mock()
    confirm = Mock(return_value=True)
    monkeypatch.setattr(cli_module, "add_turn", add_turn)
    monkeypatch.setattr(cli_module, "preflight", launch)
    monkeypatch.setattr(cli_module.typer, "confirm", confirm)

    handled = cli_module._handle_chat_preflight_intent(
        Mock(),
        conversation,
        "Should I take this trade?",
    )

    assert handled is True
    confirm.assert_called_once_with("Launch the guided preflight now?", default=True)
    launch.assert_called_once_with(
        file=None,
        session="gold-entry",
        setup_key=None,
        live_market=False,
        candle_timeframe="M5",
        candle_count=50,
        yes=False,
    )
    assert [call.args[2] for call in add_turn.call_args_list] == [
        "user",
        "assistant",
    ]
    assert all(
        call.kwargs["playbook_version_id"] == version_id
        for call in add_turn.call_args_list
    )
    assert "No broker order was placed" in add_turn.call_args_list[-1].args[3]


def test_chat_trade_intent_decline_records_turns_without_launching(
    monkeypatch,
) -> None:
    conversation = SimpleNamespace(
        name="gold-entry",
        active_playbook_version_id=uuid.uuid4(),
    )
    add_turn = Mock()
    launch = Mock()
    monkeypatch.setattr(cli_module, "add_turn", add_turn)
    monkeypatch.setattr(cli_module, "preflight", launch)
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=False))

    assert cli_module._handle_chat_preflight_intent(
        Mock(),
        conversation,
        "Review this setup before entry.",
    )

    launch.assert_not_called()
    assert "declined" in add_turn.call_args_list[-1].args[3]
    assert "No database assessment" in add_turn.call_args_list[-1].args[3]


def test_chat_returns_after_preflight_validation_exit(monkeypatch) -> None:
    conversation = SimpleNamespace(
        name="gold-entry",
        active_playbook_version_id=uuid.uuid4(),
    )
    add_turn = Mock()
    monkeypatch.setattr(cli_module, "add_turn", add_turn)
    monkeypatch.setattr(
        cli_module,
        "preflight",
        Mock(side_effect=typer.Exit(code=2)),
    )
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=True))

    assert cli_module._handle_chat_preflight_intent(
        Mock(),
        conversation,
        "I'm thinking about taking a short.",
    )

    assert "could not be completed" in add_turn.call_args_list[-1].args[3]
    assert "No order was placed" in add_turn.call_args_list[-1].args[3]


def test_health_strict_fails_when_required_check_fails(monkeypatch) -> None:
    report = HealthReport(
        (
            HealthCheck("configuration", "ok", "configured"),
            HealthCheck("database", "error", "unreachable"),
        )
    )
    monkeypatch.setattr("app.cli.check_health", Mock(return_value=report))

    result = runner.invoke(app, ["health", "--strict"])

    assert result.exit_code == 1
    assert "database" in result.stdout
    assert "unreachable" in result.stdout


def test_agent_reply_is_compact_and_details_preserve_the_audit(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, force_terminal=False, width=120),
    )

    details = cli_module._render_agent_reply(
        "# Market context\n\n**Observed:** price is inside the range.",
        "balanced · ollama/qwen3.5:9b",
        2,
        "ollama",
        "qwen3.5:9b",
        TokenUsage(input_tokens=100, output_tokens=20),
        [
            UsedReference(
                kind="broker",
                label="[untrusted markup]",
                locator="oanda:XAU_USD",
                retrieved_at="2026-07-25T12:00:00+00:00",
            )
        ],
    )

    rendered = output.getvalue()
    assert "Market context" in rendered
    assert "Observed:" in rendered
    assert "# Market context" not in rendered
    assert "**Observed:**" not in rendered
    assert "References used" not in rendered
    assert "[untrusted markup]" not in rendered
    assert "1 sources" in rendered
    assert "/details" in rendered
    assert "local" in rendered

    output.seek(0)
    output.truncate(0)
    cli_module._render_response_details(details)
    expanded = output.getvalue()
    assert "Response details" in expanded
    assert "balanced · ollama/qwen3.5:9b" in expanded
    assert "100 input" in expanded
    assert "20 output" in expanded
    assert "$0 API" in expanded
    assert "References" in expanded


def test_release_local_model_uses_the_provider_unload_boundary(monkeypatch) -> None:
    provider = object.__new__(cli_module.OllamaProvider)
    unload = Mock()
    monkeypatch.setattr(provider, "unload_model", unload)

    assert cli_module._release_local_model(provider, "qwen3.5:9b")
    unload.assert_called_once_with("qwen3.5:9b")


def test_print_model_normalizes_uuid_values(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, force_terminal=False, width=120),
    )
    identifier = uuid.uuid4()

    cli_module._print_model({"id": identifier})

    assert str(identifier) in output.getvalue()


def test_mindset_check_normalizes_phase_and_records_after_authorization(
    monkeypatch,
) -> None:
    playbook_version_id = uuid.uuid4()
    recorded = Mock(
        return_value=MindsetCheckInRead(
            id=uuid.uuid4(),
            playbook_version_id=playbook_version_id,
            trade_plan_id=None,
            trade_reference=None,
            phase="pre_trade",
            readiness=4,
            accepted_risk=True,
            emotion_tags=["focused"],
            note="Loss is accepted.",
            created_at=datetime.now(UTC),
        )
    )
    database_context = Mock()
    database_context.__enter__ = Mock(return_value=Mock())
    database_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(cli_module, "upgrade_database", Mock())
    monkeypatch.setattr(cli_module, "SessionLocal", Mock(return_value=database_context))
    monkeypatch.setattr(
        cli_module,
        "_mindset_strategy_version_id",
        Mock(return_value=playbook_version_id),
    )
    monkeypatch.setattr(cli_module, "create_mindset_check_in", recorded)

    result = runner.invoke(
        app,
        [
            "mindset",
            "check",
            "--phase",
            "pre-trade",
            "--readiness",
            "4",
            "--accepted-risk",
            "--emotion",
            "Focused",
            "--note",
            "Loss is accepted.",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    request = recorded.call_args.args[1]
    assert request.phase == "pre_trade"
    assert request.emotion_tags == ["focused"]
    assert recorded.call_args.kwargs["playbook_version_id"] == playbook_version_id
