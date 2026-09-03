import uuid
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

import app.cli as cli_module
from app.cli import app
from app.config import Settings
from app.connectors import BrokerConfigurationError
from app.costs import TokenUsage
from app.schemas import MindsetCheckInRead, TraderProfileUpsert
from app.services.agent import UsedReference
from app.services.health import HealthCheck, HealthReport
from app.services.workspaces import RequestScope

runner = CliRunner()
TEST_SCOPE = RequestScope(
    workspace_id=uuid.UUID("00000000-0000-4000-8000-000000000101"),
    account_id=uuid.UUID("00000000-0000-4000-8000-000000000102"),
)


def test_literal_terminal_text_removes_control_and_directionality_characters() -> None:
    assert cli_module._literal_terminal_text(
        "goal\x1b[31m\u202espoof\r\nnext"
    ) == "goal[31mspoof\nnext"


def test_profile_datetime_is_displayed_in_the_traders_timezone() -> None:
    rendered = cli_module._format_profile_datetime(
        datetime(2026, 1, 15, 15, 30, tzinfo=UTC),
        cli_module.ZoneInfo("America/New_York"),
    )

    assert rendered == "2026-01-15 10:30 EST"


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
        "learn",
    ):
        assert command in result.stdout


def test_api_refuses_plaintext_non_loopback_binding(monkeypatch) -> None:
    run_server = Mock()
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        Mock(return_value=Settings(trading_agent_api_key="x" * 32)),
    )
    monkeypatch.setattr(cli_module.uvicorn, "run", run_server)

    result = runner.invoke(app, ["api", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "requires direct TLS" in result.stdout
    run_server.assert_not_called()


def test_broker_sync_validates_configuration_before_mutation_confirmation(
    monkeypatch,
) -> None:
    settings = SimpleNamespace(
        broker_provider="none",
        metatrader_platform="mt5",
    )
    confirmation = Mock(return_value=False)
    monkeypatch.setattr(cli_module, "get_settings", Mock(return_value=settings))
    monkeypatch.setattr(
        cli_module,
        "create_broker_connector",
        Mock(
            side_effect=BrokerConfigurationError(
                "BROKER_PROVIDER must be oanda or metatrader for broker reads"
            )
        ),
    )
    monkeypatch.setattr(cli_module, "_confirm_agent_mutation", confirmation)

    result = runner.invoke(app, ["broker", "sync"])

    assert result.exit_code == 1
    assert "Broker setup is incomplete" in result.stdout
    assert "Choose BROKER_PROVIDER=oanda or BROKER_PROVIDER=metatrader" in result.stdout
    assert "Nothing was changed" in result.stdout
    assert "Traceback" not in result.stdout
    confirmation.assert_not_called()


def test_declining_metatrader_registration_exits_without_traceback(
    monkeypatch,
) -> None:
    class FakeConnector:
        name = "metatrader-mt5-bridge"

        async def health(self):
            return {
                "read_only": True,
                "terminal_connected": True,
            }

        async def account(self):
            return SimpleNamespace(currency="USD")

        async def aclose(self):
            return None

    settings = SimpleNamespace(
        metatrader_mode="practice",
        metatrader_platform="mt5",
    )
    monkeypatch.setattr(cli_module, "get_settings", Mock(return_value=settings))
    monkeypatch.setattr(
        cli_module,
        "create_metatrader_connector",
        Mock(return_value=FakeConnector()),
    )
    monkeypatch.setattr(
        cli_module,
        "_confirm_agent_mutation",
        Mock(return_value=False),
    )

    result = runner.invoke(
        app,
        ["broker", "configure-metatrader", "--label", "mt5-demo"],
    )

    assert result.exit_code == 0
    assert "Cancelled. Nothing was changed." in result.stdout
    assert "Traceback" not in result.stdout


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
        workspace_id=TEST_SCOPE.workspace_id,
        account_id=TEST_SCOPE.account_id,
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
    assert all(call.kwargs["playbook_version_id"] == version_id for call in add_turn.call_args_list)
    assert all(call.kwargs["scope"] == TEST_SCOPE for call in add_turn.call_args_list)
    assert "No broker order was placed" in add_turn.call_args_list[-1].args[3]


def test_chat_trade_intent_decline_records_turns_without_launching(
    monkeypatch,
) -> None:
    conversation = SimpleNamespace(
        workspace_id=TEST_SCOPE.workspace_id,
        account_id=TEST_SCOPE.account_id,
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
        workspace_id=TEST_SCOPE.workspace_id,
        account_id=TEST_SCOPE.account_id,
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


def test_chat_no_strategy_guides_recovery_then_resumes_preflight(
    monkeypatch,
) -> None:
    version_id = uuid.uuid4()
    conversation = SimpleNamespace(
        workspace_id=TEST_SCOPE.workspace_id,
        account_id=TEST_SCOPE.account_id,
        name="gold-entry",
        active_playbook_version_id=None,
    )
    add_turn = Mock()
    launch = Mock()

    def recover(_db, target):
        target.active_playbook_version_id = version_id
        return True

    monkeypatch.setattr(cli_module, "add_turn", add_turn)
    monkeypatch.setattr(
        cli_module,
        "_ensure_preflight_strategy",
        Mock(side_effect=recover),
    )
    monkeypatch.setattr(cli_module, "preflight", launch)
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=True))

    assert cli_module._handle_chat_preflight_intent(
        Mock(),
        conversation,
        "I want to take a trade, help me evaluate the setup.",
    )

    launch.assert_called_once()
    assert add_turn.call_args_list[-1].kwargs["playbook_version_id"] == version_id
    assert "completed" in add_turn.call_args_list[-1].args[3]


def test_chat_no_strategy_can_cancel_without_launching_preflight(
    monkeypatch,
) -> None:
    conversation = SimpleNamespace(
        workspace_id=TEST_SCOPE.workspace_id,
        account_id=TEST_SCOPE.account_id,
        name="gold-entry",
        active_playbook_version_id=None,
    )
    add_turn = Mock()
    launch = Mock()
    monkeypatch.setattr(cli_module, "add_turn", add_turn)
    monkeypatch.setattr(
        cli_module,
        "_ensure_preflight_strategy",
        Mock(return_value=False),
    )
    monkeypatch.setattr(cli_module, "preflight", launch)
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=True))

    assert cli_module._handle_chat_preflight_intent(
        Mock(),
        conversation,
        "Should I take this trade?",
    )

    launch.assert_not_called()
    assert "no exact strategy was selected" in add_turn.call_args_list[-1].args[3]
    assert "No database assessment" in add_turn.call_args_list[-1].args[3]


def test_preflight_strategy_recovery_can_activate_saved_strategy(
    monkeypatch,
) -> None:
    version_id = uuid.uuid4()
    conversation = SimpleNamespace(
        workspace_id=TEST_SCOPE.workspace_id,
        account_id=TEST_SCOPE.account_id,
        name="gold-entry",
        active_playbook_version_id=None,
    )
    summary = SimpleNamespace(
        name="Wyckoff Pure",
        version=2,
        knowledge_items=12,
    )
    playbook = SimpleNamespace(name="Wyckoff Pure")
    version = SimpleNamespace(
        id=version_id,
        version=2,
        content_hash="a" * 64,
    )
    authorize = Mock()

    monkeypatch.setattr(
        cli_module,
        "active_session_strategy",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        cli_module,
        "list_strategy_summaries",
        Mock(return_value=[summary]),
    )
    monkeypatch.setattr(
        cli_module,
        "_prompt_preflight_strategy_selection",
        Mock(return_value="Wyckoff Pure"),
    )
    monkeypatch.setattr(
        cli_module,
        "resolve_strategy_version",
        Mock(return_value=(playbook, version)),
    )
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=True))
    monkeypatch.setattr(cli_module, "_authorize_direct", authorize)
    monkeypatch.setattr(
        cli_module,
        "_current_scope",
        Mock(return_value=TEST_SCOPE),
    )

    def activate(_db, target, _name, *, scope, version):
        assert scope == TEST_SCOPE
        target.active_playbook_version_id = version_id
        return playbook, SimpleNamespace(version=version)

    set_strategy = Mock(side_effect=activate)
    monkeypatch.setattr(cli_module, "set_session_strategy", set_strategy)

    assert cli_module._ensure_preflight_strategy(Mock(), conversation) is True
    assert conversation.active_playbook_version_id == version_id
    authorize.assert_called_once()
    assert authorize.call_args.args[0] == "set_session_strategy"
    assert authorize.call_args.kwargs["assume_yes"] is True


def test_guided_strategy_builder_creates_preflight_ready_definition(
    monkeypatch,
) -> None:
    prompts = iter(
        (
            "NY break-retest",
            "price action",
            "Trade a confirmed retest in defined higher-timeframe context.",
            "break and retest",
            "Higher-timeframe direction and key levels are marked",
            (
                "Price breaks a defined level | "
                "Price retests and closes back with the intended direction"
            ),
            (
                "Required evidence is missing | "
                "High-impact news is inside the pre-trade window"
            ),
            "0.5",
            "2.5",
            "fear | hesitation | FOMO",
            "order block | fair value gap",
            "30",
        )
    )
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=True))

    name, description, definition, minimum_sample = (
        cli_module._prompt_guided_strategy_definition(
            SimpleNamespace(maximum_trade_risk_percent=1),
        )
    )

    assert name == "NY break-retest"
    assert description.startswith("Trade a confirmed retest")
    assert definition["setups"][0]["key"] == "break_and_retest"
    assert len(definition["setups"][0]["requirements"]) == 2
    assert definition["risk"]["maximum_risk_percent"] == "0.5"
    assert definition["risk"]["minimum_planned_r"] == "2.5"
    assert definition["forbidden_cross_strategy_concepts"] == [
        "order block",
        "fair value gap",
    ]
    assert minimum_sample == 30


def test_preflight_strategy_recovery_saves_activates_and_audits_new_strategy(
    monkeypatch,
) -> None:
    version_id = uuid.uuid4()
    conversation = SimpleNamespace(
        workspace_id=TEST_SCOPE.workspace_id,
        account_id=TEST_SCOPE.account_id,
        name="gold-entry",
        active_playbook_version_id=None,
    )
    definition = {
        "methodology": "price action",
        "objective": "Trade one explicit setup.",
        "context": {"required": ["Context is marked."], "exclusions": []},
        "setups": [
            {
                "key": "break_retest",
                "requirements": ["A retest closes in the intended direction."],
                "exclusions": ["Required evidence is missing."],
            }
        ],
        "risk": {
            "maximum_risk_percent": "0.5",
            "minimum_planned_r": "2",
            "human_confirms_every_trade": True,
        },
    }
    version = SimpleNamespace(
        id=version_id,
        version=1,
        content_hash="b" * 64,
    )
    playbook = SimpleNamespace(name="NY break-retest")
    authorize = Mock()
    create = Mock(return_value=version)

    monkeypatch.setattr(
        cli_module,
        "active_session_strategy",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        cli_module,
        "list_strategy_summaries",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        cli_module,
        "_prompt_preflight_strategy_selection",
        Mock(return_value="__create__"),
    )
    monkeypatch.setattr(
        cli_module,
        "_prompt_guided_strategy_definition",
        Mock(
            return_value=(
                "NY break-retest",
                "Trade one explicit setup.",
                definition,
                30,
            )
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        Mock(return_value=SimpleNamespace(maximum_trade_risk_percent=1)),
    )
    monkeypatch.setattr(cli_module, "_authorize_direct", authorize)
    monkeypatch.setattr(
        cli_module,
        "_current_scope",
        Mock(return_value=TEST_SCOPE),
    )
    monkeypatch.setattr(
        cli_module,
        "create_validated_strategy_version",
        create,
    )

    saved_version = version

    def activate(_db, target, _name, *, scope, version):
        assert scope == TEST_SCOPE
        assert version == saved_version.version
        target.active_playbook_version_id = version_id
        return playbook, saved_version

    monkeypatch.setattr(cli_module, "set_session_strategy", Mock(side_effect=activate))

    assert cli_module._ensure_preflight_strategy(Mock(), conversation) is True
    assert conversation.active_playbook_version_id == version_id
    assert [call.args[0] for call in authorize.call_args_list] == [
        "create_strategy_version",
        "set_session_strategy",
    ]
    assert (
        authorize.call_args_list[-1].args[1]["content_hash"]
        == version.content_hash
    )
    create.assert_called_once()


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


def test_chat_applies_automatic_migrations_before_schema_health(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        Mock(
            return_value=SimpleNamespace(
                database_auto_migrate=True,
                startup_model_smoke_test=False,
            )
        ),
    )
    monkeypatch.setattr(cli_module, "_runtime_policy", Mock(return_value=Mock()))
    monkeypatch.setattr(cli_module, "ensure_local_services", Mock(return_value=[]))
    monkeypatch.setattr(
        cli_module,
        "upgrade_database",
        Mock(side_effect=lambda: events.append("upgrade")),
    )
    monkeypatch.setattr(
        cli_module,
        "check_health",
        Mock(
            side_effect=lambda *_args, **_kwargs: (
                events.append("health")
                or HealthReport((HealthCheck("database_schema", "error", "upgrade required"),))
            )
        ),
    )
    monkeypatch.setattr(cli_module, "_render_startup_health", Mock())

    with pytest.raises(typer.Exit):
        cli_module._run_chat(None, False, None)

    assert events == ["upgrade", "health"]


def test_guided_choices_accept_display_names_aliases_and_numbers() -> None:
    assert (
        cli_module._resolve_guided_choice(
            "Trading Economics",
            cli_module.NEWS_CHOICES,
        )
        == "trading-economics"
    )
    assert (
        cli_module._resolve_guided_choice("Open AI", cli_module.MODEL_PROVIDER_CHOICES) == "openai"
    )
    assert cli_module._resolve_guided_choice("2", cli_module.BROKER_CHOICES) == "oanda"


def test_setup_accepts_human_provider_names_without_internal_slugs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / ".env"
    launcher = tmp_path / "trade"
    monkeypatch.setattr(cli_module, "install_user_launcher", Mock(return_value=launcher))
    monkeypatch.setattr(cli_module, "shell_path_hint", Mock(return_value=None))

    result = runner.invoke(
        app,
        [
            "setup",
            "--provider",
            "Open AI",
            "--database",
            "Postgres",
            "--broker",
            "OANDA v20",
            "--news",
            "Trading Economics",
            "--tradingview",
            "enabled",
            "--config",
            str(config),
            "--yes",
        ],
    )

    assert result.exit_code == 0
    content = config.read_text(encoding="utf-8")
    assert "MODEL_PROVIDER=openai" in content
    assert "DATABASE_MODE=local" in content
    assert "BROKER_PROVIDER=oanda" in content
    assert "NEWS_PROVIDER=trading-economics" in content
    assert "TRADINGVIEW_WEBHOOK_ENABLED=true" in content


def test_setup_typo_returns_suggestion_without_traceback() -> None:
    result = runner.invoke(app, ["setup", "--provider", "opnai", "--yes"])

    assert result.exit_code == 2
    assert "Did you mean" in result.stdout
    assert "OpenAI API" in result.stdout
    assert "Traceback" not in result.stdout


def test_setup_records_selected_metatrader_generation(tmp_path, monkeypatch) -> None:
    config = tmp_path / ".env"
    launcher = tmp_path / "trade"
    monkeypatch.setattr(cli_module, "install_user_launcher", Mock(return_value=launcher))
    monkeypatch.setattr(cli_module, "shell_path_hint", Mock(return_value=None))

    result = runner.invoke(
        app,
        [
            "setup",
            "--provider",
            "Open AI",
            "--database",
            "Postgres",
            "--broker",
            "MetaTrader",
            "--metatrader-platform",
            "MT4",
            "--news",
            "none",
            "--config",
            str(config),
            "--yes",
        ],
    )

    assert result.exit_code == 0
    content = config.read_text(encoding="utf-8")
    assert "BROKER_PROVIDER=metatrader" in content
    assert "METATRADER_PLATFORM=mt4" in content


def test_onboarding_validators_normalize_common_human_input() -> None:
    assert cli_module._normalize_timezone("Eastern") == "America/New_York"
    assert cli_module._normalize_timezone("yes") is None
    assert cli_module._normalize_market("gold") == "XAUUSD"
    assert cli_module._normalize_sessions(["Newyork", "asian"]) == [
        "New York",
        "Asia",
    ]
    assert cli_module._parse_risk_percent("1%", Decimal("1")) == Decimal("1")


@pytest.mark.parametrize(
    "response",
    [
        "i don't know",
        "choose one for me",
        "you pick",
    ],
)
def test_market_prompt_uses_explicit_starter_for_conversational_requests(
    response,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        Mock(return_value=response),
    )

    assert cli_module._prompt_markets(["XAUUSD"]) == ["XAUUSD"]


def test_market_normalization_does_not_turn_sentences_into_symbols() -> None:
    assert cli_module._normalize_market("choose one for me") == "CHOOSE ONE FOR ME"
    assert cli_module._normalize_market("s&p 500") == "SPX500"


def test_beginner_onboarding_defaults_are_conservative_and_guided(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_local_timezone_name",
        Mock(return_value="America/New_York"),
    )

    defaults = cli_module._clean_onboarding_defaults(
        "beginner",
        SimpleNamespace(maximum_trade_risk_percent=2),
    )

    assert defaults.timezone == "America/New_York"
    assert defaults.learning_mode == "guided"
    assert defaults.markets == ("EURUSD",)
    assert defaults.sessions == ("New York",)
    assert defaults.maximum_risk_percent == Decimal("0.5")
    assert "predefined entry, stop, and target" in defaults.trading_style
    assert "risk discipline" in defaults.goals


def test_bounded_text_reprompts_with_clear_feedback(monkeypatch) -> None:
    prompts = iter(("x" * 121, "KyleRain"))
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )

    assert (
        cli_module._prompt_bounded_text(
            "Display name",
            default="Trader",
            maximum_length=120,
            field_name="Display name",
        )
        == "KyleRain"
    )


def test_trading_style_spelling_is_reviewed_without_changing_strategy_rules(
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=True))

    assert cli_module._review_trading_style_spelling("Break and Retext") == "Break and Retest"


def test_trading_style_spelling_suggestion_can_be_declined(monkeypatch) -> None:
    monkeypatch.setattr(cli_module.typer, "confirm", Mock(return_value=False))

    assert cli_module._review_trading_style_spelling("Break and Retext") == "Break and Retext"


def test_goals_reject_irrelevant_inappropriate_text_and_reprompt(
    monkeypatch,
) -> None:
    prompts = iter(("my dick", "journal every trade"))
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )

    assert cli_module._prompt_goals(["consistency"]) == ["journal every trade"]


def test_goal_replacement_retains_valid_items_and_reprompts_only_invalid_one(
    monkeypatch,
) -> None:
    prompts = iter(
        (
            "consistency, my dick, journal every trade",
            "risk discipline",
        )
    )
    labels: list[str] = []

    def prompt(label, *_args, **_kwargs):
        labels.append(label)
        return next(prompts)

    monkeypatch.setattr(cli_module.typer, "prompt", prompt)

    assert cli_module._prompt_goals(["consistency"]) == [
        "consistency",
        "risk discipline",
        "journal every trade",
    ]
    assert labels == ["Goals", "Replacement goal"]


def test_duplicate_goal_replacement_retains_other_valid_items(monkeypatch) -> None:
    prompts = iter(("risk discipline, Risk Discipline, patience", "journal every trade"))
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )

    assert cli_module._prompt_goals(["consistency"]) == [
        "risk discipline",
        "journal every trade",
        "patience",
    ]


def test_profile_schema_rejects_unsafe_goal_outside_the_wizard() -> None:
    with pytest.raises(ValueError, match="trading, risk, learning, or process"):
        TraderProfileUpsert(
            display_name="Trader",
            timezone="America/New_York",
            trading_style="Break and retest",
            goals=["my dick"],
        )


def test_goal_rejection_does_not_repeat_a_secret(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, force_terminal=False, width=120),
    )
    prompts = iter(("API_KEY=sk-abcdefghijklmnop", "journal every trade"))
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )

    assert cli_module._prompt_goals(["consistency"]) == ["journal every trade"]
    assert "sk-abcdefghijklmnop" not in output.getvalue()


def test_session_prompt_enforces_complete_list_limits_before_review(
    monkeypatch,
) -> None:
    too_many = ",".join(f"Custom{index}" for index in range(13))
    prompts = iter((too_many, "New York"))
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )

    assert cli_module._prompt_sessions(["New York"]) == ["New York"]


def test_goal_prompt_enforces_complete_list_limits_before_review(
    monkeypatch,
) -> None:
    too_many = ",".join(f"risk goal {index}" for index in range(21))
    prompts = iter((too_many, "protect capital"))
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )

    assert cli_module._prompt_goals(["consistency"]) == ["protect capital"]


def test_prop_account_onboarding_collects_structured_challenge_rules(
    monkeypatch,
) -> None:
    prompts = iter(
        (
            "prop",
            "100K evaluation",
            "100,000",
            "usd",
            "Example Firm",
            "Phase One",
            "evaluation",
            "5%",
            "10%",
            "8%",
            "4",
            "30",
            "40%",
            "equity",
            "prohibited",
            "restricted",
            "prohibited",
            "no copy trading | close before rollover",
        )
    )
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )
    monkeypatch.setattr(
        cli_module.typer,
        "confirm",
        Mock(return_value=False),
    )

    account = cli_module._prompt_account_constraint(
        None,
        trader_timezone="America/New_York",
    )

    assert account is not None
    assert account.account_type == "prop"
    assert account.account_size == Decimal("100000")
    assert account.currency == "USD"
    assert account.firm_name == "Example Firm"
    assert account.phase == "evaluation"
    assert account.rules.maximum_daily_loss_percent == Decimal("5")
    assert account.rules.drawdown_type == "equity_based"
    assert account.rules.news_trading == "prohibited"
    assert account.rules.custom_rules == [
        "no copy trading",
        "close before rollover",
    ]


def test_beginner_personal_or_demo_account_skips_advanced_rule_questionnaire(
    monkeypatch,
) -> None:
    prompts = iter(
        (
            "demo",
            "MT5 Demo",
            "100,000",
            "USD",
        )
    )
    confirmation = Mock(side_effect=AssertionError("advanced confirmation was requested"))
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )
    monkeypatch.setattr(cli_module.typer, "confirm", confirmation)

    account = cli_module._prompt_account_constraint(
        None,
        trader_timezone="America/New_York",
        experience_level="beginner",
    )

    assert account is not None
    assert account.name == "MT5 Demo"
    assert account.account_type == "personal"
    assert account.account_size == Decimal("100000")
    assert account.rules.maximum_daily_loss_percent is None
    assert account.rules.drawdown_type == "unknown"
    confirmation.assert_not_called()


def test_onboarding_review_renders_profile_markup_literally(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, force_terminal=False, width=180),
    )

    cli_module._render_onboarding_review(
        display_name="[red]Trader[/red]",
        timezone="America/New_York",
        experience="advanced",
        markets=["XAUUSD"],
        sessions=["[green]Custom[/green]"],
        trading_style="[bold]Break and retest[/bold]",
        goals=["[red]risk discipline[/red]"],
        maximum_risk=Decimal("1"),
        account=None,
        broker="none",
        news="none",
        tradingview="disabled",
        learning_mode="disabled",
        learning_topics=[],
    )

    rendered = output.getvalue()
    assert "[red]Trader[/red]" in rendered
    assert "[green]Custom[/green]" in rendered
    assert "[bold]Break and retest[/bold]" in rendered


def test_invalid_saved_timezone_is_replaced_before_prompting(monkeypatch) -> None:
    prompts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli_module,
        "_local_timezone_name",
        Mock(return_value="America/New_York"),
    )
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda label, *, default: prompts.append((label, default)) or default,
    )

    assert cli_module._prompt_timezone("yes") == "America/New_York"
    assert prompts == [("Timezone", "America/New_York")]


def test_onboarding_accepts_typo_corrections_and_saves_only_after_review(
    monkeypatch,
) -> None:
    prompts = iter(
        (
            "KyleRain",
            "Advanced",
            "Eastern",
            "On demand",
            "all",
            "XAUUD, SPX500, NAS100",
            "Newyork, asian",
            "Wyckoff, smart money trading, and ICT",
            "consistency, profitability, pyschology",
            "2%",
            "Not configured yet",
            "OANDA",
            "Trading Economics",
            "disabled",
            "Goals",
            "journal every trade",
        )
    )
    confirmations = iter((True, True, False, True))
    update_env = Mock()
    profile_id = uuid.uuid4()
    upsert = Mock(
        return_value=SimpleNamespace(id=profile_id, display_name="KyleRain")
    )
    deactivate_account = Mock()
    configure_learning = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(
        cli_module,
        "get_trader_profile",
        Mock(return_value=SimpleNamespace(display_name="Old profile")),
    )
    monkeypatch.setattr(cli_module.typer, "prompt", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(
        cli_module.typer,
        "confirm",
        lambda *_args, **_kwargs: next(confirmations),
    )
    monkeypatch.setattr(cli_module, "update_env_file", update_env)
    monkeypatch.setattr(cli_module, "upsert_trader_profile", upsert)
    monkeypatch.setattr(
        cli_module,
        "deactivate_account_constraints",
        deactivate_account,
    )
    monkeypatch.setattr(
        cli_module,
        "configure_learning_curriculum",
        configure_learning,
    )
    monkeypatch.setattr(
        cli_module,
        "_current_scope",
        Mock(return_value=TEST_SCOPE),
    )

    saved = cli_module._run_onboarding(
        Mock(),
        SimpleNamespace(
            maximum_trade_risk_percent=1.0,
            broker_provider="none",
            news_provider="none",
        ),
    )

    assert saved is True
    profile = upsert.call_args.args[1]
    assert upsert.call_args.kwargs["scope"] == TEST_SCOPE
    assert profile.timezone == "America/New_York"
    assert profile.experience_level == "advanced"
    assert profile.markets == ["XAUUSD", "SPX500", "NAS100"]
    assert profile.sessions == ["New York", "Asia"]
    assert profile.goals == ["journal every trade"]
    assert profile.risk_preferences == {"maximum_trade_risk_percent": 2.0}
    deactivate_account.assert_called_once_with(
        upsert.call_args.args[0],
        profile_id,
        scope=TEST_SCOPE,
        commit=False,
    )
    assert configure_learning.call_args.kwargs == {
        "scope": TEST_SCOPE,
        "experience_level": "advanced",
        "teaching_mode": "on_demand",
        "selected_topics": list(cli_module.all_learning_topics()),
        "commit": False,
    }
    assert update_env.call_args.args[1] == {
        "BROKER_PROVIDER": "oanda",
        "NEWS_PROVIDER": "trading-economics",
        "MAXIMUM_TRADE_RISK_PERCENT": "2",
        "TRADINGVIEW_WEBHOOK_ENABLED": "false",
    }


def test_onboarding_review_decline_writes_nothing(monkeypatch) -> None:
    prompts = iter(
        (
            "Trader",
            "advanced",
            "America/New_York",
            "Not now",
            "XAUUSD",
            "New York",
            "Price action",
            "consistency",
            "1",
            "none",
            "none",
            "none",
            "disabled",
            "discard",
        )
    )
    update_env = Mock()
    upsert = Mock()
    monkeypatch.setattr(cli_module, "get_trader_profile", Mock(return_value=None))
    monkeypatch.setattr(cli_module.typer, "prompt", lambda *_args, **_kwargs: next(prompts))
    confirmations = iter((False, True))
    monkeypatch.setattr(
        cli_module.typer,
        "confirm",
        lambda *_args, **_kwargs: next(confirmations),
    )
    monkeypatch.setattr(cli_module, "update_env_file", update_env)
    monkeypatch.setattr(cli_module, "upsert_trader_profile", upsert)
    monkeypatch.setattr(
        cli_module,
        "_current_scope",
        Mock(return_value=TEST_SCOPE),
    )

    saved = cli_module._run_onboarding(
        Mock(),
        SimpleNamespace(
            maximum_trade_risk_percent=1.0,
            broker_provider="none",
            news_provider="none",
        ),
    )

    assert saved is False
    update_env.assert_not_called()
    upsert.assert_not_called()


def test_onboard_command_continues_directly_into_chat(monkeypatch) -> None:
    class SessionContext:
        def __enter__(self):
            return Mock()

        def __exit__(self, *_args):
            return False

    settings = SimpleNamespace()
    settings_loader = Mock(return_value=settings)
    settings_loader.cache_clear = Mock()
    run_chat = Mock()
    monkeypatch.setattr(cli_module, "get_settings", settings_loader)
    monkeypatch.setattr(cli_module, "ensure_local_services", Mock(return_value=()))
    monkeypatch.setattr(cli_module, "upgrade_database", Mock())
    monkeypatch.setattr(cli_module, "SessionLocal", Mock(return_value=SessionContext()))
    monkeypatch.setattr(cli_module, "_run_onboarding", Mock(return_value=True))
    monkeypatch.setattr(cli_module, "_run_chat", run_chat)

    cli_module.onboard_command()

    settings_loader.cache_clear.assert_called_once_with()
    run_chat.assert_called_once_with(None, False, None)


def test_account_use_restores_real_env_file_when_database_commit_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    original = (
        "OPENAI_API_KEY=keep-secret\n"
        "TRADING_WORKSPACE=old\n"
        "TRADING_ACCOUNT=00000000-0000-4000-8000-000000000001\n"
    )
    env_file.write_text(original, encoding="utf-8")
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        slug="trading",
        name="Trading",
    )
    account = SimpleNamespace(
        id=uuid.uuid4(),
        label="Primary",
        broker="OANDA",
        mode="practice",
        is_default=False,
    )
    db = Mock()
    db.commit.side_effect = RuntimeError("database commit failed")

    class SessionContext:
        def __enter__(self):
            return db

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cli_module, "upgrade_database", Mock())
    monkeypatch.setattr(cli_module, "SessionLocal", Mock(return_value=SessionContext()))
    monkeypatch.setattr(cli_module, "_configured_workspace", Mock(return_value=workspace))
    monkeypatch.setattr(cli_module, "resolve_account", Mock(return_value=account))
    monkeypatch.setattr(cli_module, "list_accounts", Mock(return_value=[account]))
    monkeypatch.setattr(cli_module, "_authorize_direct", Mock())
    monkeypatch.setattr(cli_module, "default_config_path", Mock(return_value=env_file))

    with pytest.raises(RuntimeError, match="database commit failed"):
        cli_module.account_use("Primary", yes=True)

    assert env_file.read_text(encoding="utf-8") == original
    db.rollback.assert_called_once_with()


def test_account_recover_reactivates_legacy_identity_without_moving_history(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=keep-secret\n", encoding="utf-8")
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        slug="legacy-local",
        name="Legacy",
    )
    archived = SimpleNamespace(
        id=uuid.uuid4(),
        label="Legacy / unassigned",
        broker="manual",
        mode="practice",
        active=False,
        is_default=False,
    )
    db = Mock()

    class SessionContext:
        def __enter__(self):
            return db

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cli_module, "upgrade_database", Mock())
    monkeypatch.setattr(cli_module, "SessionLocal", Mock(return_value=SessionContext()))
    monkeypatch.setattr(cli_module, "_configured_workspace", Mock(return_value=workspace))
    monkeypatch.setattr(cli_module, "resolve_account", Mock(return_value=archived))
    monkeypatch.setattr(cli_module, "list_accounts", Mock(return_value=[archived]))
    monkeypatch.setattr(cli_module, "_authorize_direct", Mock())
    monkeypatch.setattr(cli_module, "default_config_path", Mock(return_value=env_file))

    cli_module.account_recover("Legacy / unassigned", label="Imported history", yes=True)

    assert archived.active is True
    assert archived.is_default is True
    assert archived.label == "Imported history"
    content = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=keep-secret" in content
    assert f"TRADING_ACCOUNT={archived.id}" in content
    db.commit.assert_called_once_with()


def test_startup_health_explains_optional_feature_names() -> None:
    output = StringIO()
    original_console = cli_module.console
    cli_module.console = Console(file=output, force_terminal=False)
    try:
        cli_module._render_startup_health(
            HealthReport(
                (
                    HealthCheck("database", "ok", "ready"),
                    HealthCheck("api_security", "warning", "disabled"),
                    HealthCheck("web_search", "warning", "disabled"),
                )
            )
        )
    finally:
        cli_module.console = original_console

    rendered = output.getvalue()
    assert "remote API password" in rendered
    assert "broad web search" in rendered
    assert "api_security" not in rendered


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
    assert "Trading Agent ❯" in rendered
    assert "1 source" in rendered
    assert "/details" in rendered

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


def test_terminal_markdown_unwraps_documents_and_stacks_wide_tables_as_cards() -> None:
    reply = """Plan summary

```markdown
# XAUUSD example

| Option | Action required |
|---|---|
| Use the example | Fill the values later |
| Ask a question | Clarify one rule |
```
"""

    rendered = cli_module._terminal_markdown(reply)

    assert "```markdown" not in rendered
    assert "| Option |" not in rendered
    assert "### Use the example" in rendered
    assert "**Next step**\nFill the values later" in rendered
    assert "Fill the values later\n\n### Ask a question" in rendered
    assert rendered.count("### XAUUSD example") == 1


def test_terminal_markdown_humanizes_internal_jargon_but_preserves_code() -> None:
    rendered = cli_module._terminal_markdown(
        "Unavailable ❌ per [edge_requires_evidence]. "
        "Use validate_strategy_draft before create_strategy_version.\n\n"
        "```bash\ntrade knowledge search edge_requires_evidence\n```"
    )

    assert "Unavailable —" in rendered
    assert "evidence requirement" in rendered
    assert "strategy review" in rendered
    assert "strategy save" in rendered
    assert "trade knowledge search edge_requires_evidence" in rendered


def test_terminal_markdown_uses_plain_labels_and_deduplicates_status_words() -> None:
    rendered = cli_module._terminal_markdown(
        "| Phase | Hypothetical Description | Encoding |\n"
        "|---|---|---|\n"
        "| Accumulation | Range hypothesis. | Need: measurable boundaries. |\n\n"
        "| Element | Current State This Session Window |\n"
        "|---|---|\n"
        "| Market data | ❌ Disabled until configured. |\n"
        "| Journal | ✅ Available after confirmation. |"
    )

    assert "**Working idea**" in rendered
    assert "**What must be defined**\nmeasurable boundaries." in rendered
    assert "**Status**" in rendered
    assert "Unavailable — until configured." in rendered
    assert "Available — after confirmation." in rendered
    assert "Available — Available" not in rendered


def test_terminal_markdown_preserves_actual_command_fences_and_removes_controls() -> None:
    rendered = cli_module._terminal_markdown("Run this:\u200b\n\n```bash\ntrade health\n```")

    assert "\u200b" not in rendered
    assert "```bash\ntrade health\n```" in rendered


def test_terminal_markdown_spaces_dense_clarifying_questions() -> None:
    rendered = cli_module._terminal_markdown(
        "Daily structure or H4 structure? Keep the higher-timeframe thesis explicit. "
        "M5 or M15 entries? Choose one trigger timeframe for testing. "
        "Fixed stop or ATR stop? Define one deterministic calculation."
    )

    assert (
        "Keep the higher-timeframe thesis explicit.\n\nM5 or M15 entries?" in rendered
    )
    assert "Choose one trigger timeframe for testing.\n\nFixed stop or ATR stop?" in rendered


def test_terminal_markdown_does_not_reflow_commands_when_spacing_questions() -> None:
    rendered = cli_module._terminal_markdown(
        "Which workflow? Why now?\n\n"
        "```bash\ntrade strategy use wyckoff-pure? --dry-run?\n```"
    )

    assert "Which workflow?\n\nWhy now?" in rendered
    assert "trade strategy use wyckoff-pure? --dry-run?" in rendered


def test_agent_reply_splits_the_audit_footer_on_narrow_terminals(
    monkeypatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli_module,
        "console",
        Console(file=output, force_terminal=False, width=60),
    )

    cli_module._render_agent_reply(
        "A short answer.",
        "balanced · ollama/qwen3.5:35b-a3b",
        1,
        "ollama",
        "qwen3.5:35b-a3b",
        TokenUsage(),
        [],
        {"total_seconds": 8.2, "output_tokens_per_second": 32.4},
    )

    rendered = output.getvalue()
    assert "qwen3.5:35b-a3b · balanced\n" in rendered
    assert "8.2s · 32.4 tok/s · 0 sources · /details" in rendered


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
            workspace_id=TEST_SCOPE.workspace_id,
            account_id=TEST_SCOPE.account_id,
            playbook_version_id=playbook_version_id,
            trade_plan_id=None,
            trade_reference=None,
            phase="pre_trade",
            readiness=4,
            accepted_risk=True,
            emotion_tags=["focused"],
            emotional_state="I’m fucking ready.",
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
    monkeypatch.setattr(
        cli_module,
        "_current_scope",
        Mock(return_value=TEST_SCOPE),
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
            "--emotional-state",
            "I’m fucking ready.",
            "--note",
            "Loss is accepted.",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    request = recorded.call_args.args[1]
    assert request.phase == "pre_trade"
    assert request.emotion_tags == ["focused"]
    assert request.emotional_state == "I’m fucking ready."
    assert recorded.call_args.kwargs["scope"] == TEST_SCOPE
    assert recorded.call_args.kwargs["playbook_version_id"] == playbook_version_id


def test_pretrade_mindset_prompt_reprompts_and_preserves_exact_language(
    monkeypatch,
) -> None:
    prompts = iter(
        (
            "9",
            "3",
            "Fear | fear | Anger",
            "I’m fucking terrified.",
            "I can still follow the plan.",
        )
    )
    monkeypatch.setattr(
        cli_module.typer,
        "prompt",
        lambda *_args, **_kwargs: next(prompts),
    )
    monkeypatch.setattr(cli_module.typer, "confirm", lambda *_args, **_kwargs: True)

    request = cli_module._prompt_pretrade_mindset()

    assert request.readiness == 3
    assert request.accepted_risk is True
    assert request.emotion_tags == ["fear", "anger"]
    assert request.emotional_state == "I’m fucking terrified."


def test_mindset_check_does_not_echo_rejected_credential(
    monkeypatch,
) -> None:
    upgrade = Mock()
    monkeypatch.setattr(cli_module, "upgrade_database", upgrade)
    secret = "OPENAI_API_KEY=sk-abcdefghijklmnop"

    result = runner.invoke(
        app,
        [
            "mindset",
            "check",
            "--emotional-state",
            secret,
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert secret not in result.output
    assert "was not accepted" in result.output
    upgrade.assert_not_called()
