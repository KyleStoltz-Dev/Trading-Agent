import importlib.util
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.providers import ProviderConfigurationError, resolve_provider_name

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
) -> HealthReport:
    checks = [
        HealthCheck(
            name="configuration",
            status="ok",
            detail=f"environment={settings.app_env}; provider={settings.model_provider}",
        )
    ]

    try:
        provider_name = resolve_provider_name(settings)
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
    except ProviderConfigurationError as exc:
        checks.append(
            HealthCheck(
                "model_provider",
                "warning",
                f"{exc}; chat and chart analysis are unavailable",
            )
        )

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

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks.append(HealthCheck("database", "ok", "database connection succeeded"))
    except Exception as exc:
        checks.append(
            HealthCheck(
                "database",
                "error",
                f"database connection failed: {type(exc).__name__}",
            )
        )

    return HealthReport(tuple(checks))
