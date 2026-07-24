from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://trading:trading@localhost:5432/trading_agent"
    model_provider: Literal["auto", "openai", "anthropic"] = "auto"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-sol"
    openai_safety_identifier: str = "trading-agent-local"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    app_env: str = "development"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
