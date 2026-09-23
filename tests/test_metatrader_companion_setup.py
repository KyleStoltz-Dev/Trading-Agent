from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import app.cli as cli
from app.config import Settings
from app.metatrader_pairing import Pairing


@pytest.mark.parametrize("matching_server", [True, False])
def test_pairing_verifies_identity_and_keeps_mutation_confirmation(monkeypatch, matching_server):
    pairing = Pairing(
        account="123456",
        server="Synthetic-Demo",
        symbol="XAUUSD",
        port=8766,
        token="synthetic-private-test-token-123456",
    )
    monkeypatch.setattr("app.metatrader_pairing.read_pairing", lambda _: pairing)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings())

    class Connector:
        name = "metatrader-mt5-bridge"

        async def health(self):
            return {
                "read_only": True,
                "terminal_connected": True,
                "transport": "mql-companion",
                "broker_server": pairing.server if matching_server else "Wrong-Server",
            }

        async def account(self):
            return SimpleNamespace(currency="USD", external_account_id=pairing.account)

        async def aclose(self):
            return None

    create = Mock(return_value=Connector())
    monkeypatch.setattr(cli, "create_metatrader_connector", create)
    mutation = Mock(return_value=False)
    monkeypatch.setattr(cli, "_confirm_agent_mutation", mutation)
    database = Mock()
    monkeypatch.setattr(cli, "upgrade_database", database)
    result = CliRunner().invoke(
        cli.app,
        [
            "broker",
            "configure-metatrader",
            "--label",
            "demo",
            "--companion-preset",
            "/synthetic/private.set",
        ],
    )
    assert result.exit_code == (0 if matching_server else 1)
    settings = create.call_args.args[0]
    assert settings.metatrader_bridge_url == "http://127.0.0.1:8766"
    assert settings.metatrader_account_id.get_secret_value() == pairing.account
    assert settings.metatrader_bridge_token.get_secret_value() == pairing.token
    assert "MetaTrader bridge token" not in result.stdout
    assert pairing.token not in result.stdout
    assert mutation.call_count == (1 if matching_server else 0)
    database.assert_not_called()


def test_pairing_requires_private_file_and_never_echoes_it(monkeypatch):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(
        "app.metatrader_pairing.read_pairing", Mock(side_effect=ValueError("private"))
    )
    create = Mock()
    monkeypatch.setattr(cli, "create_metatrader_connector", create)
    result = CliRunner().invoke(
        cli.app,
        [
            "broker",
            "configure-metatrader",
            "--label",
            "demo",
            "--companion-preset",
            "/synthetic/private.set",
        ],
    )
    assert result.exit_code != 0
    create.assert_not_called()
