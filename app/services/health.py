import importlib.util
import shutil
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import LEGACY_ENV_BACKEND, Settings, secret_value
from app.connectors.factory import (
    BrokerConfigurationError,
    validate_broker_account_selection,
)
from app.db import inspect_schema
from app.models import BrokerConnection, TradingAccount
from app.providers import ProviderConfigurationError, resolve_provider_name
from app.providers.ollama_provider import OllamaProvider
from app.services.secrets import SecretBackendError, validate_secret_backend
from app.services.tradingview import trusted_proxy_networks
from app.services.web_fetch import (
    WebFetchError,
    allowed_domain_paths,
    allowed_domains,
)
from app.services.workspaces import RequestScope, resolve_current_scope
from app.system_resources import GIB, assess_model_fit, resource_snapshot

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
    scope: RequestScope | None = None,
) -> HealthReport:
    checks = [
        HealthCheck(
            name="configuration",
            status="ok",
            detail=f"environment={settings.app_env}; provider={settings.model_provider}",
        )
    ]
    try:
        resources = resource_snapshot()
    except (OSError, RuntimeError) as exc:
        checks.append(
            HealthCheck(
                "system_resources",
                "warning",
                f"cross-platform resource telemetry unavailable: {type(exc).__name__}",
            )
        )
        resources = None
    else:
        swap = (
            f"{resources.swap_percent:.1f}%"
            if resources.swap_percent is not None
            else "unknown"
        )
        checks.append(
            HealthCheck(
                "system_resources",
                "ok",
                (
                    f"{resources.platform}; "
                    f"{resources.available_memory_bytes / GIB:.1f} GiB available / "
                    f"{resources.total_memory_bytes / GIB:.1f} GiB total; "
                    f"memory={resources.memory_percent:.1f}%; "
                    f"swap={swap}; "
                    f"disk={resources.disk_free_bytes / GIB:.1f} GiB free"
                ),
            )
        )
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

    if not settings.tradingview_webhook_enabled:
        checks.append(
            HealthCheck(
                "tradingview",
                "ok",
                "TradingView webhook receiver intentionally disabled",
            )
        )
    else:
        try:
            networks = trusted_proxy_networks(
                settings.tradingview_trusted_proxy_cidrs
            )
        except ValueError as exc:
            checks.append(
                HealthCheck(
                    "tradingview",
                    "error",
                    f"invalid trusted proxy boundary: {exc}",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    "tradingview",
                    "warning",
                    (
                        "receiver enabled for "
                        f"{len(networks)} trusted proxy network(s); verify public "
                        "proxy mTLS, source-IP allowlisting, rate limiting, and "
                        "header stripping"
                    ),
                )
            )

    oanda_values = (settings.oanda_api_token, settings.oanda_account_id)
    metatrader_values = (
        settings.metatrader_bridge_token,
        settings.metatrader_account_id,
    )
    if settings.broker_secret_backend != LEGACY_ENV_BACKEND:
        if (
            settings.broker_provider == "none"
            and settings.deployment_mode == "local-single-user"
        ):
            checks.append(
                HealthCheck(
                    "broker-secrets",
                    "ok",
                    "per-account secret backend selected; no broker needs it yet",
                )
            )
        else:
            try:
                validate_secret_backend(settings)
            except SecretBackendError as exc:
                checks.append(HealthCheck("broker-secrets", "error", str(exc)))
            else:
                checks.append(
                    HealthCheck(
                        "broker-secrets",
                        "ok",
                        f"per-account {settings.broker_secret_backend} secret backend ready",
                    )
                )
    elif settings.broker_provider == "oanda" and any(oanda_values) and not all(oanda_values):
        checks.append(
            HealthCheck(
                "oanda",
                "error",
                "OANDA token and account id must be configured together",
            )
        )
    elif settings.broker_provider == "metatrader" and any(metatrader_values) and not all(
        metatrader_values
    ):
        checks.append(
            HealthCheck(
                "metatrader",
                "error",
                "MetaTrader bridge token and account id must be configured together",
            )
        )
    elif (
        settings.broker_provider == "metatrader"
        and settings.metatrader_bridge_token is not None
        and len(secret_value(settings.metatrader_bridge_token) or "") < 32
    ):
        checks.append(
            HealthCheck(
                "metatrader",
                "error",
                "MetaTrader bridge token must contain at least 32 characters",
            )
        )
    elif settings.broker_provider == "none":
        checks.append(
            HealthCheck(
                "broker",
                "ok",
                "broker connector intentionally disabled",
            )
        )
    elif settings.broker_provider == "oanda" and all(oanda_values):
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
    elif settings.broker_provider == "oanda":
        checks.append(
            HealthCheck(
                "oanda",
                "warning",
                "OANDA is selected but its read-only token and account id are not configured",
            )
        )
    elif all(metatrader_values):
        bridge_url = urlsplit(settings.metatrader_bridge_url)
        bridge_is_remote_http = (
            bridge_url.scheme.casefold() == "http"
            and (bridge_url.hostname or "").casefold()
            not in {"127.0.0.1", "::1", "localhost"}
        )
        insecure = bridge_is_remote_http and settings.metatrader_allow_insecure_remote
        checks.append(
            HealthCheck(
                "metatrader",
                "warning" if settings.metatrader_mode == "live" or insecure else "ok",
                (
                    f"read-only {settings.metatrader_platform.upper()} bridge configured"
                    + (" for a LIVE account" if settings.metatrader_mode == "live" else "")
                    + (" over explicitly allowed remote HTTP" if insecure else "")
                ),
            )
        )
    else:
        checks.append(
            HealthCheck(
                "metatrader",
                "warning",
                "MetaTrader is selected but its bridge token and account id are not configured",
            )
        )

    if settings.news_provider == "none":
        checks.append(
            HealthCheck(
                "news",
                "ok",
                "news connector intentionally disabled",
            )
        )
    elif settings.trading_economics_api_key:
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
                "Trading Economics is selected but its API key is not configured",
            )
        )

    if settings.web_fetch_enabled:
        try:
            domains = allowed_domains(settings.web_fetch_allowed_domains)
            if not domains:
                raise WebFetchError("at least one documented domain is required")
            path_policies = allowed_domain_paths(
                settings.web_fetch_allowed_paths
            )
            missing_paths = domains - path_policies.keys()
            if missing_paths:
                raise WebFetchError(
                    "documented path policy missing for: "
                    + ", ".join(sorted(missing_paths))
                )
        except WebFetchError as exc:
            checks.append(
                HealthCheck(
                    "allowlisted_web",
                    "error",
                    f"invalid WEB_FETCH_ALLOWED_DOMAINS: {exc}",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    "allowlisted_web",
                    "ok",
                    f"confirmed read-only web fetch enabled for {len(domains)} "
                    "domains with documented path constraints",
                )
            )
    else:
        checks.append(
            HealthCheck(
                "allowlisted_web",
                "warning",
                "read-only allowlisted web fetch is disabled",
            )
        )

    if settings.web_search_provider == "brave":
        if secret_value(settings.brave_search_api_key):
            checks.append(
                HealthCheck(
                    "web_search",
                    "ok",
                    "tier-3 Brave Search is configured",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    "web_search",
                    "error",
                    "WEB_SEARCH_PROVIDER=brave requires BRAVE_SEARCH_API_KEY",
                )
            )
    else:
        checks.append(
            HealthCheck(
                "web_search",
                "warning",
                "tier-3 broad web search is disabled",
            )
        )

    try:
        provider_name = resolve_provider_name(settings)
        if provider_name == "ollama":
            provider = OllamaProvider(settings)
            try:
                model_sizes = provider.installed_model_sizes()
                installed = frozenset(model_sizes)
                loaded = provider.loaded_models()
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
                configured_profiles = {
                    settings.ollama_economy_model or settings.ollama_model,
                    settings.ollama_balanced_model or settings.ollama_model,
                    settings.ollama_deep_model or settings.ollama_model,
                }
                missing_profiles = sorted(configured_profiles - installed)
                checks.append(
                    HealthCheck(
                        "model_profiles",
                        "warning" if missing_profiles else "ok",
                        (
                            "configured Ollama models not installed: "
                            + ", ".join(missing_profiles)
                            if missing_profiles
                            else "all configured Ollama routing profiles are installed"
                        ),
                    )
                )
                if settings.resource_aware_model_routing and resources is not None:
                    assessments = [
                        assess_model_fit(
                            model=model,
                            model_size_bytes=model_sizes[model],
                            context_length=settings.ollama_context_length,
                            memory_reserve_gb=settings.model_memory_reserve_gb,
                            memory_block_percent=settings.model_memory_block_percent,
                            swap_block_percent=settings.model_swap_block_percent,
                            currently_loaded=model in loaded,
                            snapshot=resources,
                        )
                        for model in configured_profiles
                        if model_sizes.get(model, 0) > 0
                    ]
                    blocked = [
                        assessment.model
                        for assessment in assessments
                        if assessment.status == "block"
                    ]
                    cautious = [
                        assessment.model
                        for assessment in assessments
                        if assessment.status == "warning"
                    ]
                    status: CheckStatus = (
                        "warning" if blocked or cautious else "ok"
                    )
                    if blocked:
                        detail = (
                            "currently blocked by memory/swap pressure: "
                            + ", ".join(sorted(blocked))
                            + "; automatic routing will use a safer configured model"
                        )
                    elif cautious:
                        detail = (
                            "currently near preferred memory reserve: "
                            + ", ".join(sorted(cautious))
                        )
                    else:
                        detail = (
                            "all installed configured profiles fit current pressure "
                            "with the configured reserve"
                        )
                    checks.append(
                        HealthCheck("model_capacity", status, detail)
                    )
                elif not settings.resource_aware_model_routing:
                    checks.append(
                        HealthCheck(
                            "model_capacity",
                            "warning",
                            "resource-aware local-model routing is disabled",
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
                    selected_scope = scope
                    if selected_scope is None:
                        try:
                            selected_scope = resolve_current_scope(
                                session,
                                workspace_reference=settings.trading_workspace,
                                account_reference=settings.trading_account,
                            )
                        except LookupError as exc:
                            checks.append(
                                HealthCheck(
                                    "account_scope",
                                    "error",
                                    f"{exc}; run `trade onboard` or `trade account list`",
                                )
                            )
                    connections = (
                        []
                        if selected_scope is None
                        else list(
                            session.scalars(
                                select(BrokerConnection).where(
                                    BrokerConnection.workspace_id
                                    == selected_scope.workspace_id,
                                    BrokerConnection.account_id
                                    == selected_scope.account_id,
                                )
                            )
                        )
                    )
                    selected_account = (
                        None
                        if selected_scope is None
                        else session.scalar(
                            select(TradingAccount).where(
                                TradingAccount.workspace_id
                                == selected_scope.workspace_id,
                                TradingAccount.id == selected_scope.account_id,
                            )
                        )
                    )
                    if settings.tradingview_webhook_enabled:
                        if (
                            selected_account is None
                            or not selected_account.tradingview_webhook_secret_sha256
                        ):
                            checks.append(
                                HealthCheck(
                                    "tradingview_account_secret",
                                    "error",
                                    "selected account has no TradingView webhook "
                                    "secret; run `trade account tradingview-secret`",
                                )
                            )
                        else:
                            checks.append(
                                HealthCheck(
                                    "tradingview_account_secret",
                                    "ok",
                                    "selected account has a TradingView webhook secret",
                                )
                            )
                    if (
                        settings.broker_provider != "none"
                        and selected_account is not None
                    ):
                        expected_provider = (
                            "oanda-v20"
                            if settings.broker_provider == "oanda"
                            else f"metatrader-{settings.metatrader_platform}-bridge"
                        )
                        selected_connection = next(
                            (
                                item
                                for item in connections
                                if item.provider == expected_provider
                            ),
                            None,
                        )
                        try:
                            validate_broker_account_selection(
                                settings,
                                selected_account,
                                selected_connection,
                            )
                        except BrokerConfigurationError as exc:
                            checks.append(
                                HealthCheck(
                                    "broker_account_scope",
                                    "error",
                                    str(exc),
                                )
                            )
                        else:
                            checks.append(
                                HealthCheck(
                                    "broker_account_scope",
                                    "ok",
                                    "broker credentials and connection match the "
                                    "selected account",
                                )
                            )
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
