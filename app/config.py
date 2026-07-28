import hashlib
import os
import stat
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

LOCAL_VAULT_BACKEND = "keyring"
HOSTED_VAULT_BACKEND = "external"
LEGACY_ENV_BACKEND = "legacy-env"


def parse_ollama_model_digests(value: str) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        model, separator, digest = item.partition("=")
        normalized = digest.casefold()
        if (
            not separator
            or not model.strip()
            or not normalized.startswith("sha256:")
            or len(normalized) != 71
            or any(character not in "0123456789abcdef" for character in normalized[7:])
        ):
            raise ValueError(
                "OLLAMA_MODEL_DIGESTS must contain comma-separated "
                "MODEL=sha256:<64 lowercase hex characters> entries"
            )
        mappings[model.strip()] = normalized
    return mappings


def _default_ollama_runtime_lock_path() -> Path:
    user_scope = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:12]
    return (
        Path(tempfile.gettempdir())
        / f"trading-agent-{user_scope}"
        / "ollama-runtime.lock"
    )


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql+psycopg://trading:trading@localhost:5432/trading_agent",
        repr=False,
    )
    database_mode: Literal["local", "neon", "custom"] = "local"
    deployment_mode: Literal["local-single-user", "hosted-multi-user"] = (
        "local-single-user"
    )
    broker_secret_backend: Literal["keyring", "external", "legacy-env"] = (
        LOCAL_VAULT_BACKEND
    )
    broker_external_secret_backend: str | None = None
    hosted_principal_auth_enabled: bool = False
    hosted_rls_enforced: bool = False
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
    ollama_allow_insecure_remote: bool = False
    ollama_model_digests: str = ""
    ollama_manage_remote_runtime: bool = False
    ollama_model: str = "qwen3.5:9b"
    ollama_economy_model: str | None = None
    ollama_balanced_model: str | None = None
    ollama_deep_model: str | None = None
    ollama_context_length: int = Field(default=16384, ge=2048, le=262144)
    ollama_keep_alive: str = "2m"
    ollama_unload_on_exit: bool = True
    ollama_request_timeout_seconds: float = Field(default=300.0, gt=0, le=1800)
    resource_aware_model_routing: bool = True
    model_memory_reserve_gb: float = Field(default=6.0, ge=2, le=64)
    model_memory_block_percent: float = Field(default=92.0, ge=70, le=99)
    model_swap_block_percent: float = Field(default=80.0, ge=50, le=100)
    ollama_runtime_lock_path: Path = Field(
        default_factory=_default_ollama_runtime_lock_path
    )
    ollama_runtime_lock_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    startup_model_smoke_test: bool = True
    local_service_autostart: bool = True
    postgres_service_name: str = Field(
        default="postgresql@17",
        pattern=r"^[A-Za-z0-9@._+-]+$",
    )
    agent_mode: Literal["auto", "economy", "balanced", "deep"] = "auto"
    model_max_concurrent_requests: int = Field(default=2, ge=1, le=16)
    model_request_queue_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    model_history_turn_limit: int = Field(default=20, ge=2, le=100)
    app_env: Literal["development", "test", "production"] = "development"
    database_auto_migrate: bool = True
    trading_agent_api_key: SecretStr | None = None
    trading_workspace: str = "legacy-local"
    trading_account: str | None = None
    api_confirmation_ttl_seconds: int = Field(default=60, ge=10, le=300)
    api_max_request_bytes: int = Field(
        default=12 * 1024 * 1024,
        ge=1024,
        le=32 * 1024 * 1024,
    )
    api_requests_per_minute: int = Field(default=120, ge=1, le=1200)
    tradingview_webhook_enabled: bool = False
    tradingview_webhook_max_request_bytes: int = Field(
        default=32 * 1024,
        ge=1024,
        le=256 * 1024,
    )
    tradingview_webhook_requests_per_minute: int = Field(default=60, ge=1, le=600)
    tradingview_webhook_max_delivery_age_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
    )
    tradingview_webhook_future_skew_seconds: int = Field(
        default=60,
        ge=0,
        le=300,
    )
    tradingview_trusted_proxy_cidrs: str = "127.0.0.1/32,::1/128"
    evidence_directory: Path = Path(".data/evidence")
    maximum_trade_risk_percent: float = Field(default=1.0, gt=0, le=5)
    broker_provider: Literal["none", "oanda", "metatrader"] = "none"
    oanda_api_token: SecretStr | None = None
    oanda_account_id: SecretStr | None = None
    oanda_environment: Literal["practice", "live"] = "practice"
    oanda_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    metatrader_bridge_url: str = "http://127.0.0.1:8765"
    metatrader_bridge_token: SecretStr | None = None
    metatrader_account_id: SecretStr | None = None
    metatrader_platform: Literal["mt4", "mt5"] = "mt5"
    metatrader_mode: Literal["practice", "live"] = "practice"
    metatrader_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    metatrader_poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    metatrader_max_response_bytes: int = Field(
        default=2_000_000,
        ge=16_384,
        le=16_000_000,
    )
    metatrader_allow_insecure_remote: bool = False
    market_quote_max_age_seconds: float = Field(default=5.0, gt=0, le=300)
    trading_economics_api_key: SecretStr | None = None
    news_provider: Literal["none", "trading-economics"] = "none"
    startup_news_sync: bool = True
    startup_news_horizon_days: int = Field(default=7, ge=1, le=14)
    startup_news_min_refresh_minutes: int = Field(default=15, ge=1, le=1440)
    pretrade_news_window_minutes: int = Field(default=120, ge=15, le=1440)
    pretrade_minimum_event_importance: int = Field(default=2, ge=0, le=3)
    web_fetch_enabled: bool = True
    web_fetch_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    web_fetch_max_bytes: int = Field(default=1_000_000, ge=1_024, le=5_000_000)
    web_fetch_max_text_characters: int = Field(
        default=30_000,
        ge=1_000,
        le=100_000,
    )
    web_fetch_allowed_domains: str = (
        "oanda.com,tradingview.com,cmegroup.com,federalreserve.gov,"
        "fred.stlouisfed.org,bls.gov,bea.gov,tradingeconomics.com"
    )
    web_fetch_allowed_paths: str = (
        "oanda.com=/,/us-en/,/rest-live-v20/;"
        "tradingview.com=/,/support/,/symbols/;"
        "cmegroup.com=/,/education/,/markets/,/market-data/;"
        "federalreserve.gov=/,/newsevents/,/monetarypolicy/;"
        "fred.stlouisfed.org=/,/series/,/categories/,/releases/;"
        "bls.gov=/,/news.release/,/schedule/;"
        "bea.gov=/,/news/,/data/;"
        "tradingeconomics.com=/,/calendar/,/articles/"
    )
    chart_allowed_roots: str = ""
    web_search_provider: Literal["disabled", "brave"] = "disabled"
    brave_search_api_key: SecretStr | None = None
    web_search_max_results: int = Field(default=5, ge=1, le=10)
    development_enabled: bool = False
    development_acknowledge_host_filesystem_read_risk: bool = False
    development_repository: Path = Path(".")
    development_base_ref: str = "HEAD"
    development_backend: Literal["codex"] = "codex"
    development_approval_flow: Literal["scope_only", "scope_and_diff"] = "scope_and_diff"
    development_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    development_state_directory: Path = Path(".data/development")

    model_config = SettingsConfigDict(extra="ignore")

    @model_validator(mode="after")
    def enforce_security_invariants(self) -> "Settings":
        if self.development_enabled:
            if self.app_env.casefold() != "development":
                raise ValueError(
                    "DEVELOPMENT_ENABLED is allowed only when APP_ENV=development"
                )
            if not self.development_acknowledge_host_filesystem_read_risk:
                raise ValueError(
                    "DEVELOPMENT_ENABLED requires "
                    "DEVELOPMENT_ACKNOWLEDGE_HOST_FILESYSTEM_READ_RISK=true; "
                    "Codex workspace-write limits writes but is not a filesystem-read "
                    "or container boundary, and staged Codex authentication may be "
                    "readable by child tools"
                )
        if (
            self.broker_secret_backend == LEGACY_ENV_BACKEND
            and self.deployment_mode != "local-single-user"
        ):
            raise ValueError("BROKER_SECRET_BACKEND=legacy-env is local-only")
        if self.deployment_mode == "hosted-multi-user":
            missing: list[str] = []
            if self.app_env != "production":
                missing.append("APP_ENV=production")
            if not self.hosted_principal_auth_enabled:
                missing.append("HOSTED_PRINCIPAL_AUTH_ENABLED=true")
            if not self.hosted_rls_enforced:
                missing.append("HOSTED_RLS_ENFORCED=true")
            if self.broker_secret_backend != HOSTED_VAULT_BACKEND:
                missing.append("BROKER_SECRET_BACKEND=external")
            if not self.broker_external_secret_backend:
                missing.append("BROKER_EXTERNAL_SECRET_BACKEND")
            if self.tradingview_webhook_enabled:
                missing.append("TRADINGVIEW_WEBHOOK_ENABLED=false")
            if self.database_auto_migrate:
                missing.append("DATABASE_AUTO_MIGRATE=false")
            if missing:
                raise ValueError(
                    "hosted-multi-user requires authenticated principals, PostgreSQL "
                    "row-level security, and external per-account secrets; missing: "
                    + ", ".join(missing)
                )
        url = make_url(self.database_url)
        host = (url.host or "").casefold()
        if host not in {"", "127.0.0.1", "::1", "localhost"}:
            sslmode = str(url.query.get("sslmode", "")).casefold()
            if sslmode not in {"require", "verify-ca", "verify-full"}:
                raise ValueError(
                    "remote DATABASE_URL connections require sslmode=require, "
                    "verify-ca, or verify-full"
                )
            if self.app_env == "production" and sslmode != "verify-full":
                raise ValueError(
                    "production remote DATABASE_URL connections require "
                    "sslmode=verify-full to authenticate the database server"
                )
        digests = parse_ollama_model_digests(self.ollama_model_digests)
        uses_ollama = self.model_provider == "ollama" or (
            self.model_provider == "auto"
            and self.openai_api_key is None
            and self.anthropic_api_key is None
        )
        if self.app_env.casefold() == "production" and uses_ollama:
            configured_models = {
                model
                for model in (
                    self.ollama_model,
                    self.ollama_economy_model,
                    self.ollama_balanced_model,
                    self.ollama_deep_model,
                )
                if model
            }
            missing = sorted(configured_models - digests.keys())
            if missing:
                raise ValueError(
                    "production Ollama requires exact OLLAMA_MODEL_DIGESTS entries "
                    "for: " + ", ".join(missing)
                )
        return self


def _trusted_config_file(path: Path, *, allow_missing: bool = False) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValueError("configuration paths must be absolute")
    if candidate.is_symlink():
        raise ValueError(f"configuration file cannot be a symlink: {candidate}")
    try:
        metadata = candidate.stat()
    except FileNotFoundError:
        if allow_missing:
            return candidate.resolve()
        raise
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"configuration path is not a regular file: {candidate}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError(
            f"configuration file must be owned by the current user: {candidate}"
        )
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise PermissionError(
            f"configuration file permissions are too broad; run chmod 600 {candidate}"
        )
    return candidate.resolve()


def environment_files() -> tuple[Path, ...]:
    explicit = os.environ.get("TRADING_AGENT_CONFIG")
    if explicit:
        return (_trusted_config_file(Path(explicit), allow_missing=True),)

    package_project = Path(__file__).resolve().parent.parent / ".env"
    user_config = Path.home() / ".config" / "trading-agent" / ".env"
    # Load exactly one trusted source. Merging dotenv files can retain a secret from
    # one file while accepting an endpoint override from another. The process
    # environment remains the explicit, highest-precedence override.
    for candidate in (user_config, package_project):
        if candidate.exists() or candidate.is_symlink():
            return (_trusted_config_file(candidate),)
    return ()


def default_config_path() -> Path:
    existing = environment_files()
    if existing:
        return existing[0]
    return Path.home() / ".config" / "trading-agent" / ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=environment_files() or None)


def secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None
