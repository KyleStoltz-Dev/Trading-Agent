import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import LEGACY_ENV_BACKEND, Settings, secret_value
from app.connectors.factory import (
    create_broker_connector,
    create_metatrader_connector,
    create_news_connector,
    create_oanda_connector,
)
from app.integration_catalog import integration_options
from app.models import (
    AccountSnapshot,
    BrokerConnection,
    EconomicEvent,
    NewsItem,
    TradingViewAlert,
)
from app.services.tradingview import trusted_proxy_networks
from app.services.web_search import search_brave
from app.services.workspaces import RequestScope, validate_scope

VerificationState = Literal[
    "configured",
    "incomplete",
    "not configured",
    "not applicable",
]
ReachabilityState = Literal[
    "verified now",
    "verified previously",
    "not tested",
    "inbound only",
    "unavailable",
    "not applicable",
]
EvidenceState = Literal[
    "observed",
    "not observed",
    "not applicable",
]
VerificationSource = Literal["none", "simulated", "real", "stored-real"]


@dataclass(frozen=True)
class IntegrationVerification:
    key: str
    kind: str
    name: str
    implementation: str
    configuration: VerificationState
    reachability: ReachabilityState
    evidence: EvidenceState
    last_success_at: datetime | None
    detail: str
    next_action: str | None = None
    verification_source: VerificationSource = "none"


def _latest_broker_connection(
    db: Session,
    *,
    scope: RequestScope,
    provider: str,
) -> BrokerConnection | None:
    return db.scalar(
        select(BrokerConnection)
        .where(
            BrokerConnection.workspace_id == scope.workspace_id,
            BrokerConnection.account_id == scope.account_id,
            BrokerConnection.provider == provider,
        )
        .order_by(BrokerConnection.created_at.desc())
        .limit(1)
    )


def _credential_state(*values: str | None) -> VerificationState:
    present = tuple(bool(value) for value in values)
    if all(present):
        return "configured"
    if any(present):
        return "incomplete"
    return "not configured"


def _broker_report(
    db: Session,
    *,
    scope: RequestScope,
    scoped_external_account_id: str,
    key: str,
    kind: str,
    name: str,
    provider: str,
    selected: bool,
    credentials: tuple[str | None, ...],
    external_account_id: str | None,
) -> IntegrationVerification:
    configuration = _credential_state(*credentials)
    if (
        external_account_id
        and external_account_id != scoped_external_account_id
    ):
        configuration = "incomplete"
    connection = _latest_broker_connection(
        db,
        scope=scope,
        provider=provider,
    )
    snapshot_at = (
        db.scalar(
            select(func.max(AccountSnapshot.retrieved_at)).where(
                AccountSnapshot.workspace_id == scope.workspace_id,
                AccountSnapshot.account_id == connection.account_id,
                AccountSnapshot.source == provider,
            )
        )
        if connection
        else None
    )
    last_success = max(
        (
            item
            for item in (
                connection.last_healthy_at if connection else None,
                snapshot_at,
            )
            if item is not None
        ),
        default=None,
    )
    previously_verified = last_success is not None
    selected_text = "selected" if selected else "available but not selected"
    if configuration == "configured":
        detail = f"Read-only credentials are complete; provider is {selected_text}."
        next_action = (
            None
            if previously_verified or not selected
            else "Run `trade integrations --verify-live` to test read-only access."
        )
    elif configuration == "incomplete":
        if external_account_id and external_account_id != scoped_external_account_id:
            detail = (
                "Configured credentials target a different account than the "
                f"selected workspace account; provider is {selected_text}."
            )
            next_action = "Select the matching account or update the provider settings."
        else:
            detail = (
                "Some required settings exist, but configuration is incomplete; "
                f"{selected_text}."
            )
            next_action = "Complete the provider settings without placing secrets in chat."
    else:
        detail = f"No read-only credentials are configured; provider is {selected_text}."
        next_action = (
            "Use `trade onboard` or the provider setup command."
            if selected
            else None
        )
    return IntegrationVerification(
        key=key,
        kind=kind,
        name=name,
        implementation="implemented",
        configuration=configuration,
        reachability="verified previously" if previously_verified else "not tested",
        evidence="observed" if previously_verified else "not observed",
        last_success_at=last_success,
        detail=detail,
        next_action=next_action,
        verification_source="stored-real" if previously_verified else "none",
    )


def integration_verifications(
    settings: Settings,
    db: Session,
    *,
    scope: RequestScope,
) -> tuple[IntegrationVerification, ...]:
    """Describe implementation, configuration, and observed provider evidence separately."""
    scoped_account = validate_scope(db, scope)
    reports: list[IntegrationVerification] = []
    for option in integration_options():
        if option.status == "planned":
            reports.append(
                IntegrationVerification(
                    key=option.key,
                    kind=option.kind,
                    name=option.name,
                    implementation="planned",
                    configuration="not applicable",
                    reachability="not applicable",
                    evidence="not applicable",
                    last_success_at=None,
                    detail="Adapter is documented but has not been implemented.",
                )
            )
            continue
        if option.key == "oanda":
            connection = _latest_broker_connection(
                db, scope=scope, provider="oanda-v20"
            )
            legacy_account_id = (
                secret_value(settings.oanda_account_id)
                if settings.broker_secret_backend == LEGACY_ENV_BACKEND
                else None
            )
            reports.append(
                _broker_report(
                    db,
                    scope=scope,
                    scoped_external_account_id=scoped_account.external_account_id,
                    key=option.key,
                    kind=option.kind,
                    name=option.name,
                    provider="oanda-v20",
                    selected=settings.broker_provider == "oanda",
                    credentials=(
                        connection.config_reference
                        if connection
                        else secret_value(settings.oanda_api_token)
                        if legacy_account_id
                        else None,
                        scoped_account.external_account_id
                        if connection
                        else legacy_account_id,
                    ),
                    external_account_id=(
                        scoped_account.external_account_id
                        if connection
                        else legacy_account_id
                    ),
                )
            )
            continue
        if option.key == "metatrader":
            provider = f"metatrader-{settings.metatrader_platform}-bridge"
            connection = _latest_broker_connection(db, scope=scope, provider=provider)
            legacy_account_id = (
                secret_value(settings.metatrader_account_id)
                if settings.broker_secret_backend == LEGACY_ENV_BACKEND
                else None
            )
            reports.append(
                _broker_report(
                    db,
                    scope=scope,
                    scoped_external_account_id=scoped_account.external_account_id,
                    key=option.key,
                    kind=option.kind,
                    name=option.name,
                    provider=provider,
                    selected=settings.broker_provider == "metatrader",
                    credentials=(
                        connection.config_reference
                        if connection
                        else secret_value(settings.metatrader_bridge_token)
                        if legacy_account_id
                        else None,
                        scoped_account.external_account_id
                        if connection
                        else legacy_account_id,
                    ),
                    external_account_id=(
                        scoped_account.external_account_id
                        if connection
                        else legacy_account_id
                    ),
                )
            )
            continue
        if option.key in {"trading-economics", "forex-factory"}:
            configuration = (
                _credential_state(secret_value(settings.trading_economics_api_key))
                if option.key == "trading-economics"
                else "configured"
                if settings.news_provider == "forex-factory"
                else "not configured"
            )
            calendar_at = db.scalar(
                select(func.max(EconomicEvent.retrieved_at)).where(
                    EconomicEvent.source == option.key
                )
            )
            news_at = db.scalar(
                select(func.max(NewsItem.retrieved_at)).where(
                    NewsItem.source == option.key
                )
            )
            last_success = max(
                (item for item in (calendar_at, news_at) if item is not None),
                default=None,
            )
            observed = last_success is not None
            reports.append(
                IntegrationVerification(
                    key=option.key,
                    kind=option.kind,
                    name=option.name,
                    implementation="implemented",
                    configuration=configuration,
                    reachability="verified previously" if observed else "not tested",
                    evidence="observed" if observed else "not observed",
                    last_success_at=last_success,
                    detail=(
                        "The public weekly calendar feed is selected; no API key is required."
                        if option.key == "forex-factory"
                        and configuration == "configured"
                        else "Forex Factory is available but is not selected."
                        if option.key == "forex-factory"
                        else "Calendar/news credentials are complete."
                        if configuration == "configured"
                        else "Calendar/news credentials are not configured."
                    ),
                    next_action=(
                        None
                        if observed or settings.news_provider != option.key
                        else (
                            "Configure the provider, then run "
                            "`trade integrations --verify-live`."
                        )
                    ),
                    verification_source="stored-real" if observed else "none",
                )
            )
            continue
        if option.key == "tradingview":
            if settings.tradingview_webhook_enabled:
                try:
                    trusted_proxy_networks(settings.tradingview_trusted_proxy_cidrs)
                except ValueError:
                    tradingview_configuration: VerificationState = "incomplete"
                else:
                    tradingview_configuration = "configured"
            else:
                tradingview_configuration = "not configured"
            last_success = db.scalar(
                select(func.max(TradingViewAlert.received_at)).where(
                    TradingViewAlert.workspace_id == scope.workspace_id,
                    TradingViewAlert.account_id == scope.account_id,
                    TradingViewAlert.verification_method.in_(
                        (
                            "account_secret_proxy_mtls_source_ip",
                            "trusted_proxy_mtls_and_source_ip",
                        )
                    )
                )
            )
            observed = last_success is not None
            reports.append(
                IntegrationVerification(
                    key=option.key,
                    kind=option.kind,
                    name=option.name,
                    implementation="implemented",
                    configuration=tradingview_configuration,
                    reachability=(
                        "verified previously" if observed else "inbound only"
                    ),
                    evidence="observed" if observed else "not observed",
                    last_success_at=last_success,
                    detail=(
                        "An authenticated inbound alert has been accepted."
                        if observed
                        else "Only a real inbound TradingView delivery can verify this path."
                    ),
                    next_action=(
                        None
                        if observed or not settings.tradingview_webhook_enabled
                        else (
                            "Expose the webhook securely, enable it, and send a "
                            "TradingView test alert."
                        )
                    ),
                    verification_source="stored-real" if observed else "none",
                )
            )
            continue
    brave_key = secret_value(settings.brave_search_api_key)
    search_configuration = (
        _credential_state(brave_key)
        if settings.web_search_provider == "brave"
        else "not configured"
    )
    reports.append(
        IntegrationVerification(
            key="brave",
            kind="web-search",
            name="Brave Search",
            implementation="implemented",
            configuration=search_configuration,
            reachability="not tested",
            evidence="not observed",
            last_success_at=None,
            detail=(
                "Tier-3 broad search is selected and its API key is configured."
                if search_configuration == "configured"
                else "Tier-3 broad search is disabled or missing its API key."
            ),
            next_action=(
                "Add BRAVE_SEARCH_API_KEY, then run live verification."
                if settings.web_search_provider == "brave"
                and search_configuration != "configured"
                else None
            ),
        )
    )
    return tuple(reports)


async def verify_live_integrations(
    settings: Settings,
    reports: tuple[IntegrationVerification, ...],
    *,
    db: Session | None = None,
    scope: RequestScope | None = None,
) -> tuple[IntegrationVerification, ...]:
    """Run bounded read-only provider probes without persisting returned data."""
    verified: list[IntegrationVerification] = []
    for report in reports:
        if report.configuration != "configured":
            verified.append(report)
            continue
        try:
            if report.key == "oanda":
                if db is not None and scope is not None:
                    account = validate_scope(db, scope)
                    connection = _latest_broker_connection(
                        db, scope=scope, provider="oanda-v20"
                    )
                    connector = create_broker_connector(
                        settings, account=account, connection=connection
                    )
                else:
                    connector = create_oanda_connector(settings)
                try:
                    account, positions = await asyncio.gather(
                        connector.account(),
                        connector.positions(),
                    )
                finally:
                    await connector.aclose()
                expected = (
                    account.external_account_id
                    if db is not None and scope is not None
                    else secret_value(settings.oanda_account_id)
                )
                if account.external_account_id != expected:
                    raise ValueError("provider returned a different account")
                detail = (
                    f"Read-only account and positions endpoints responded; "
                    f"{len(positions)} open position(s)."
                )
            elif report.key == "metatrader":
                if db is not None and scope is not None:
                    account = validate_scope(db, scope)
                    connection = _latest_broker_connection(
                        db,
                        scope=scope,
                        provider=f"metatrader-{settings.metatrader_platform}-bridge",
                    )
                    connector = create_broker_connector(
                        settings, account=account, connection=connection
                    )
                else:
                    connector = create_metatrader_connector(settings)
                try:
                    health = await connector.health()
                    account, positions = await asyncio.gather(
                        connector.account(),
                        connector.positions(),
                    )
                finally:
                    await connector.aclose()
                if health.get("read_only") is not True:
                    raise ValueError("bridge did not attest read-only mode")
                expected = (
                    account.external_account_id
                    if db is not None and scope is not None
                    else secret_value(settings.metatrader_account_id)
                )
                if account.external_account_id != expected:
                    raise ValueError("provider returned a different account")
                detail = (
                    f"Read-only bridge, account, and positions endpoints responded; "
                    f"{len(positions)} open position(s)."
                )
            elif report.key in {"trading-economics", "forex-factory"}:
                connector = create_news_connector(settings)
                try:
                    events, headlines = await asyncio.gather(
                        connector.calendar(
                            start=date.today(),
                            end=date.today(),
                            countries=("United States",),
                            minimum_importance=2,
                        ),
                        connector.news(limit=1),
                    )
                finally:
                    await connector.aclose()
                detail = (
                    f"Calendar feed responded; {len(events)} event(s)"
                    + (
                        f", {len(headlines)} headline(s)."
                        if report.key == "trading-economics"
                        else ". Forex Factory does not provide a headline API."
                    )
                )
            elif report.key == "brave":
                response = await asyncio.to_thread(
                    search_brave,
                    "official current financial market calendar",
                    api_key=secret_value(settings.brave_search_api_key) or "",
                    max_results=1,
                )
                detail = f"Search endpoint responded; {len(response.results)} result(s)."
            else:
                verified.append(report)
                continue
        except Exception as exc:
            verified.append(
                replace(
                    report,
                    reachability="unavailable",
                    detail=f"Read-only verification failed ({type(exc).__name__}).",
                    next_action="Run `trade health` and verify this provider's settings.",
                    verification_source="real",
                )
            )
        else:
            verified.append(
                replace(
                    report,
                    reachability="verified now",
                    evidence="observed",
                    last_success_at=datetime.now(UTC),
                    detail=detail,
                    next_action=None,
                    verification_source="real",
                )
            )
    return tuple(verified)


async def verify_simulated_integrations(
    reports: tuple[IntegrationVerification, ...],
) -> tuple[IntegrationVerification, ...]:
    """Exercise deterministic adapters without claiming provider reachability."""
    from app.services.integration_simulator import DeterministicIntegrationSimulator

    simulator = DeterministicIntegrationSimulator()
    verified: list[IntegrationVerification] = []
    try:
        for report in reports:
            try:
                if report.key == "oanda":
                    connector = simulator.oanda()
                    account, positions = await asyncio.gather(
                        connector.account(),
                        connector.positions(),
                    )
                    if account.external_account_id != simulator.account_id:
                        raise ValueError("simulator returned a different OANDA account")
                    detail = (
                        "Simulated OANDA account/positions passed; "
                        f"{len(positions)} position(s). Real credentials were not tested."
                    )
                elif report.key == "metatrader":
                    connector = simulator.metatrader()
                    health, account, positions = await asyncio.gather(
                        connector.health(),
                        connector.account(),
                        connector.positions(),
                    )
                    if (
                        health.get("read_only") is not True
                        or account.external_account_id != simulator.account_id
                    ):
                        raise ValueError("simulator MetaTrader attestation failed")
                    detail = (
                        "Simulated MetaTrader health/account/positions passed; "
                        f"{len(positions)} position(s). A real terminal was not tested."
                    )
                elif report.key == "trading-economics":
                    connector = simulator.trading_economics()
                    events, headlines = await asyncio.gather(
                        connector.calendar(
                            start=date(2026, 7, 27),
                            end=date(2026, 7, 27),
                            countries=("United States",),
                            minimum_importance=2,
                        ),
                        connector.news(limit=1),
                    )
                    detail = (
                        "Simulated calendar/news passed; "
                        f"{len(events)} event(s), {len(headlines)} headline(s). "
                        "The real provider was not tested."
                    )
                elif report.key == "tradingview":
                    accepted = await simulator.tradingview(
                        event_id="verification-1",
                        secret=simulator.webhook_secret,
                    )
                    replay = await simulator.tradingview(
                        event_id="verification-1",
                        secret=simulator.webhook_secret,
                    )
                    if not accepted.accepted or not replay.duplicate:
                        raise ValueError("simulator TradingView replay handling failed")
                    detail = (
                        "Simulated authenticated webhook and replay passed. "
                        "Public HTTPS and a real TradingView delivery were not tested."
                    )
                else:
                    verified.append(report)
                    continue
            except Exception as exc:
                verified.append(
                    replace(
                        report,
                        reachability="unavailable",
                        detail=(
                            "Simulated verification failed "
                            f"({type(exc).__name__}); no real provider was contacted."
                        ),
                        verification_source="simulated",
                    )
                )
            else:
                verified.append(
                    replace(
                        report,
                        reachability="verified now",
                        evidence="observed",
                        last_success_at=datetime.now(UTC),
                        detail=detail,
                        next_action=(
                            "Run live verification with real credentials before relying "
                            "on provider availability."
                        ),
                        verification_source="simulated",
                    )
                )
    finally:
        await simulator.aclose()
    return tuple(verified)
