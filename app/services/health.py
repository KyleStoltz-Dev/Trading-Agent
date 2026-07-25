import importlib.util
import shutil
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings, secret_value
from app.db import inspect_schema
from app.models import BrokerConnection
from app.providers import ProviderConfigurationError, resolve_provider_name
from app.providers.ollama_provider import OllamaProvider

if TYPE_CHECKING:
    from app.policy import PolicyEngine

CheckStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class HealthReport:
    checks: tuple[HealthCheck, ...]

    @property
    def ready(self) -> bool:
        return not any(check.status == "error" for check in self.checks)

    def model_dump(self) -> dict:
        return {
            "ready": self.ready,
            "checks": [asdict(check) for check in self.checks],
        }


def check_health(
    settings: Settings,
    engine: Engine,
    policy: "PolicyEngine | None" = None,
    *,
    model_smoke_test: bool = False,
) -> HealthReport:
    checks = [
        HealthCheck(
            name="configuration",
            status="ok",
            detail=f"environment={settings.app_env}; provider={settings.model_provider}",
        )
    ]
    if settings.trading_agent_api_key is None:
        checks.append(
            HealthCheck(
                "api_security",
                "warning",
                "API disabled until TRADING_AGENT_API_KEY is configured",
            )
        )
    elif len(secret_value(settings.trading_agent_api_key) or "") < 32:
        checks.append(
            HealthCheck(
                "api_security",
                "error",
                "TRADING_AGENT_API_KEY must contain at least 32 characters",
            )
        )
    else:
        checks.append(HealthCheck("api_security", "ok", "API key is configured"))

    oanda_values = (settings.oanda_api_token, settings.oanda_account_id)
    if all(oanda_values):
        checks.append(
            HealthCheck(
                "oanda",
                "warning" if settings.oanda_environment == "live" else "ok",
                (
                    "read-only adapter points to a LIVE account"
                    if settings.oanda_environment == "live"
                    else "read-only adapter configured for practice"
                ),
            )
        )
    elif any(oanda_values):
        checks.append(
            HealthCheck(
                "oanda",
                "error",
                "OANDA token and account id must be configured together",
            )
        )
    else:
        checks.append(
            HealthCheck(
                "oanda",
                "warning",
                "OANDA read-only data is not configured",
            )
        )

    if settings.trading_economics_api_key:
        checks.append(
            HealthCheck(
                "news",
                "ok",
                "Trading Economics read-only connector is configured",
            )
        )
    else:
        checks.append(
            HealthCheck(
                "news",
                "warning",
                "economic calendar and news sync are not configured",
            )
        )

    try:
        provider_name = resolve_provider_name(settings)
        if provider_name == "ollama":
            provider = OllamaProvider(settings)
            try:
                installed = provider.installed_models()
                if settings.ollama_model in installed and model_smoke_test:
                    provider.smoke_test()
            finally:
                provider.client.close()
            if settings.ollama_model in installed:
                checks.append(
                    HealthCheck(
                        "model_provider",
                        "ok",
                        f"ollama/{settings.ollama_model} is installed locally",
                    )
                )
                if model_smoke_test:
                    checks.append(
                        HealthCheck(
                            "model_inference",
                            "ok",
                            "local model generated a response",
                        )
                    )
            else:
                checks.append(
                    HealthCheck(
                        "model_provider",
                        "warning",
                        (
                            f"ollama is running but {settings.ollama_model} is not installed; "
                            f"run `ollama pull {settings.ollama_model}`"
                        ),
                    )
                )
        else:
            model = (
                settings.openai_model
                if provider_name == "openai"
                else settings.anthropic_model
            )
            package_available = importlib.util.find_spec(provider_name) is not None
            checks.append(
                HealthCheck(
                    "model_provider",
                    "ok" if package_available else "warning",
                    (
                        f"{provider_name}/{model} is configured"
                        if package_available
                        else f"{provider_name}/{model} configured; install the optional adapter"
                    ),
                )
            )
    except (ProviderConfigurationError, RuntimeError) as exc:
        checks.append(
            HealthCheck(
                "model_provider",
                "error" if model_smoke_test else "warning",
                f"{exc}; chat and chart analysis are unavailable",
            )
        )

    if settings.development_enabled:
        codex = shutil.which("codex")
        repository = settings.development_repository.expanduser().resolve()
        if codex is None:
            checks.append(
                HealthCheck(
                    "development",
                    "warning",
                    "development handoff enabled but Codex CLI is not on PATH",
                )
            )
        elif not (repository / ".git").exists():
            checks.append(
                HealthCheck(
                    "development",
                    "warning",
                    f"development repository is not a Git worktree: {repository}",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    "development",
                    "ok",
                    f"Codex CLI available; isolated repository={repository}",
                )
            )
    else:
        checks.append(HealthCheck("development", "warning", "development handoff disabled"))

    try:
        from app.policy import PolicyEngine

        loaded_policy = policy or PolicyEngine.load()
        loaded_policy.assert_unchanged()
        checks.append(
            HealthCheck(
                "runtime_policy",
                "ok",
                f"version={loaded_policy.version}; sha256={loaded_policy.short_hash}",
            )
        )
    except Exception as exc:
        checks.append(
            HealthCheck(
                "runtime_policy",
                "error",
                f"runtime policy failed to load: {type(exc).__name__}",
            )
        )

    database_available = False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_available = True
        checks.append(HealthCheck("database", "ok", "database connection succeeded"))
    except Exception as exc:
        checks.append(
            HealthCheck(
                "database",
                "error",
                f"database connection failed: {type(exc).__name__}",
            )
        )

    if database_available:
        try:
            state = inspect_schema(engine)
        except Exception as exc:
            checks.append(
                HealthCheck(
                    "database_schema",
                    "error",
                    f"schema inspection failed: {type(exc).__name__}",
                )
            )
        else:
            if state.legacy_unmanaged:
                checks.append(
                    HealthCheck(
                        "database_schema",
                        "error",
                        "legacy schema requires `trading-agent db adopt-legacy`",
                    )
                )
            elif not state.current:
                checks.append(
                    HealthCheck(
                        "database_schema",
                        "error",
                        f"revision={state.current_revision}; expected={state.head_revision}",
                    )
                )
            else:
                checks.append(
                    HealthCheck(
                        "database_schema",
                        "ok",
                        f"revision={state.current_revision}",
                    )
                )
                with Session(engine) as session:
                    connections = list(session.scalars(select(BrokerConnection)))
                for connection in connections:
                    detail = f"{connection.provider}/{connection.environment}: {connection.status}"
                    if connection.last_healthy_at is not None:
                        detail += (
                            f"; last healthy={connection.last_healthy_at.isoformat()}"
                        )
                    checks.append(
                        HealthCheck(
                            f"broker_connection:{connection.provider}",
                            (
                                "ok"
                                if connection.status == "healthy"
                                else "warning"
                                if connection.status in {"configured", "disabled"}
                                else "error"
                            ),
                            detail,
                        )
                    )

    return HealthReport(tuple(checks))
