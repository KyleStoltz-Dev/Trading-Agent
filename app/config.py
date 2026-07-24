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
    model_provider: Literal["auto", "openai", "anthropic"] = "auto"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-sol"
    openai_safety_identifier: str = "trading-agent-local"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"
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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None
