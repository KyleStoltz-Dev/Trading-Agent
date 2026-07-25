import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql+psycopg://trading:trading@localhost:5432/trading_agent",
        repr=False,
    )
    model_provider: Literal["auto", "openai", "anthropic", "ollama"] = "auto"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-sol"
    openai_economy_model: str | None = None
    openai_balanced_model: str | None = None
    openai_deep_model: str | None = None
    openai_safety_identifier: str = "trading-agent-local"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"
    anthropic_economy_model: str | None = None
    anthropic_balanced_model: str | None = None
    anthropic_deep_model: str | None = None
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_allow_remote: bool = False
    ollama_model: str = "qwen3.5:9b"
    ollama_economy_model: str | None = None
    ollama_balanced_model: str | None = None
    ollama_deep_model: str | None = None
    ollama_context_length: int = Field(default=16384, ge=2048, le=262144)
    ollama_keep_alive: str = "10m"
    ollama_request_timeout_seconds: float = Field(default=300.0, gt=0, le=1800)
    startup_model_smoke_test: bool = True
    local_service_autostart: bool = True
    postgres_service_name: str = Field(
        default="postgresql@17",
        pattern=r"^[A-Za-z0-9@._+-]+$",
    )
    agent_mode: Literal["auto", "economy", "balanced", "deep"] = "auto"
    app_env: str = "development"
    database_auto_migrate: bool = True
    trading_agent_api_key: SecretStr | None = None
    evidence_directory: Path = Path(".data/evidence")
    maximum_trade_risk_percent: float = Field(default=1.0, gt=0, le=5)
    oanda_api_token: SecretStr | None = None
    oanda_account_id: SecretStr | None = None
    oanda_environment: Literal["practice", "live"] = "practice"
    oanda_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    market_quote_max_age_seconds: float = Field(default=5.0, gt=0, le=300)
    trading_economics_api_key: SecretStr | None = None
    development_enabled: bool = True
    development_repository: Path = Path(".")
    development_base_ref: str = "HEAD"
    development_backend: Literal["codex"] = "codex"
    development_approval_flow: Literal["scope_only", "scope_and_diff"] = "scope_and_diff"
    development_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    development_state_directory: Path = Path(".data/development")

    model_config = SettingsConfigDict(extra="ignore")


def environment_files() -> tuple[Path, ...]:
    explicit = os.environ.get("TRADING_AGENT_CONFIG")
    if explicit:
        return (Path(explicit).expanduser().resolve(),)

    package_project = Path(__file__).resolve().parent.parent / ".env"
    user_config = Path.home() / ".config" / "trading-agent" / ".env"
    current = Path.cwd() / ".env"
    candidates = (package_project, user_config, current)
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def default_config_path() -> Path:
    existing = environment_files()
    if existing:
        return existing[-1]
    return Path.home() / ".config" / "trading-agent" / ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=environment_files() or None)


def secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None
