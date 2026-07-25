from unittest.mock import Mock

from typer.testing import CliRunner

from app.cli import app
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
        "plan",
        "review",
        "chart",
        "api",
        "journal",
        "sessions",
        "develop",
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
