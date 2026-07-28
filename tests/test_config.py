from pathlib import Path

import pytest
from pydantic import ValidationError

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


def test_untrusted_working_directory_env_is_never_loaded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    working = tmp_path / "downloaded-project"
    working.mkdir()
    (working / ".env").write_text("METATRADER_BRIDGE_URL=http://attacker.invalid")
    monkeypatch.chdir(working)
    monkeypatch.delenv("TRADING_AGENT_CONFIG", raising=False)

    assert working / ".env" not in environment_files()


def test_user_config_is_the_only_dotenv_source(monkeypatch, tmp_path: Path) -> None:
    user_config = tmp_path / "home" / ".config" / "trading-agent" / ".env"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("MODEL_PROVIDER=ollama")
    user_config.chmod(0o600)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("TRADING_AGENT_CONFIG", raising=False)

    assert environment_files() == (user_config.resolve(),)


def test_config_rejects_broad_permissions(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "agent.env"
    config.write_text("MODEL_PROVIDER=ollama")
    config.chmod(0o644)
    monkeypatch.setenv("TRADING_AGENT_CONFIG", str(config))

    with pytest.raises(PermissionError, match="chmod 600"):
        environment_files()


def test_config_rejects_symbolic_links(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "private.env"
    target.write_text("MODEL_PROVIDER=ollama")
    target.chmod(0o600)
    link = tmp_path / "agent.env"
    link.symlink_to(target)
    monkeypatch.setenv("TRADING_AGENT_CONFIG", str(link))

    with pytest.raises(ValueError, match="symlink"):
        environment_files()


def test_remote_database_requires_encrypted_transport() -> None:
    with pytest.raises(ValidationError, match="sslmode"):
        Settings(database_url="postgresql+psycopg://user:password@db.example/agent")

    settings = Settings(
        database_url=(
            "postgresql+psycopg://user:password@db.example/agent"
            "?sslmode=verify-full"
        )
    )
    assert settings.database_url.endswith("sslmode=verify-full")


def test_production_remote_database_requires_verify_full() -> None:
    with pytest.raises(ValidationError, match="sslmode=verify-full"):
        Settings(
            app_env="production",
            model_provider="openai",
            openai_api_key="test-only-key",
            database_url=(
                "postgresql+psycopg://user:password@db.example/agent"
                "?sslmode=require"
            ),
        )

    settings = Settings(
        app_env="production",
        model_provider="openai",
        openai_api_key="test-only-key",
        database_url=(
            "postgresql+psycopg://user:password@db.example/agent"
            "?sslmode=verify-full"
        ),
    )
    assert settings.app_env == "production"


def test_development_mode_is_opt_in() -> None:
    assert Settings().development_enabled is False
    assert (
        Settings().development_acknowledge_host_filesystem_read_risk
        is False
    )


def test_development_mode_requires_explicit_host_read_risk_acknowledgment() -> None:
    with pytest.raises(ValidationError, match="filesystem-read or container boundary"):
        Settings(development_enabled=True)

    settings = Settings(
        development_enabled=True,
        development_acknowledge_host_filesystem_read_risk=True,
    )
    assert settings.development_enabled is True


def test_development_mode_is_rejected_outside_development_environment() -> None:
    with pytest.raises(ValidationError, match="APP_ENV=development"):
        Settings(
            app_env="production",
            development_enabled=True,
            development_acknowledge_host_filesystem_read_risk=True,
        )


def test_hosted_multi_user_mode_fails_closed_without_rls_and_identity() -> None:
    with pytest.raises(ValidationError, match="authenticated principals"):
        Settings(deployment_mode="hosted-multi-user")


def test_hosted_multi_user_requires_complete_security_stack() -> None:
    settings = Settings(
        deployment_mode="hosted-multi-user",
        app_env="production",
        model_provider="openai",
        openai_api_key="test-only",
        database_url=(
            "postgresql+psycopg://runtime@example.test/trading"
            "?sslmode=verify-full"
        ),
        broker_secret_backend="external",
        broker_external_secret_backend="deployment.secrets:create_backend",
        hosted_principal_auth_enabled=True,
        hosted_rls_enforced=True,
        database_auto_migrate=False,
    )
    assert settings.deployment_mode == "hosted-multi-user"


def test_legacy_broker_environment_credentials_are_explicit_and_local_only() -> None:
    assert Settings().broker_secret_backend == "keyring"
    with pytest.raises(ValidationError, match="legacy-env is local-only"):
        Settings(
            deployment_mode="hosted-multi-user",
            broker_secret_backend="legacy-env",
        )


def test_production_ollama_requires_exact_model_digest() -> None:
    with pytest.raises(ValidationError, match="OLLAMA_MODEL_DIGESTS"):
        Settings(app_env="production", model_provider="ollama")

    digest = "sha256:" + ("a" * 64)
    settings = Settings(
        app_env="production",
        model_provider="ollama",
        ollama_model_digests=f"qwen3.5:9b={digest}",
    )
    assert settings.ollama_model_digests.endswith(digest)
