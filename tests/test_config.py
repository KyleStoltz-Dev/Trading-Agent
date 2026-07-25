from pathlib import Path

from app.config import Settings, default_config_path, environment_files


def test_explicit_config_path_wins(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "agent.env"
    monkeypatch.setenv("TRADING_AGENT_CONFIG", str(config))

    assert environment_files() == (config.resolve(),)
    assert default_config_path() == config.resolve()


def test_default_config_uses_standard_user_location_when_none_exist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("TRADING_AGENT_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr("app.config.environment_files", lambda: ())

    assert default_config_path() == tmp_path / "home" / ".config" / "trading-agent" / ".env"


def test_local_model_residency_defaults_release_memory_promptly() -> None:
    settings = Settings()

    assert settings.ollama_keep_alive == "2m"
    assert settings.ollama_unload_on_exit is True
