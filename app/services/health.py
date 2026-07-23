from dataclasses import asdict, dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings

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


def check_health(settings: Settings, engine: Engine) -> HealthReport:
    checks = [
        HealthCheck(
            name="configuration",
            status="ok",
            detail=f"environment={settings.app_env}; model={settings.openai_model}",
        )
    ]

    if settings.openai_api_key:
        checks.append(
            HealthCheck("openai", "ok", "OPENAI_API_KEY is configured (not displayed)")
        )
    else:
        checks.append(
            HealthCheck(
                "openai",
                "warning",
                "OPENAI_API_KEY is missing; chat and chart analysis are unavailable",
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
