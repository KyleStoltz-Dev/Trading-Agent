import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import Settings
from app.market_data.contracts import AccountState
from app.models import (
    AccountSnapshot,
    BrokerConnection,
    EconomicEvent,
    TradingAccount,
    TradingViewAlert,
)
from app.services.integration_verification import (
    IntegrationVerification,
    integration_verifications,
    verify_live_integrations,
    verify_simulated_integrations,
)


def _report(reports, key: str):
    return next(report for report in reports if report.key == key)


def test_qualification_does_not_confuse_implemented_with_live_verified(
    db_session,
    workspace_account,
    request_scope,
) -> None:
    _, account = workspace_account
    reports = integration_verifications(
        Settings(
            _env_file=None,
            broker_secret_backend="legacy-env",
            broker_provider="oanda",
            oanda_api_token="read-only-token",
            oanda_account_id=account.external_account_id,
            news_provider="trading-economics",
            trading_economics_api_key="calendar-key",
        ),
        db_session,
        scope=request_scope,
    )

    oanda = _report(reports, "oanda")
    assert oanda.implementation == "implemented"
    assert oanda.configuration == "configured"
    assert oanda.reachability == "not tested"
    assert oanda.evidence == "not observed"

    for key in ("ibkr", "alpaca", "twelve-data", "ctrader"):
        report = _report(reports, key)
        assert report.implementation == "planned"
        assert report.configuration == "not applicable"
        assert report.reachability == "not applicable"
        assert report.evidence == "not applicable"
    assert _report(reports, "ctrader").implementation == "planned"
    assert _report(reports, "ctrader").configuration == "not applicable"
    assert _report(reports, "trading-economics").evidence == "not observed"


def test_qualification_uses_accepted_evidence_and_exact_broker_account(
    db_session,
    workspace_account,
    request_scope,
) -> None:
    now = datetime.now(UTC)
    workspace, account = workspace_account
    account.broker = "OANDA"
    account.external_account_id = "account-verified"
    account.label = "verified"
    other_account = TradingAccount(
        workspace_id=workspace.id,
        broker="OANDA",
        external_account_id="account-other",
        label="other",
        currency="USD",
        mode="practice",
    )
    db_session.add_all((account, other_account))
    db_session.flush()
    db_session.add_all(
        (
            BrokerConnection(
                workspace_id=workspace.id,
                account_id=account.id,
                provider="oanda-v20",
                environment="practice",
                status="configured",
            ),
            BrokerConnection(
                workspace_id=workspace.id,
                account_id=other_account.id,
                provider="oanda-v20",
                environment="practice",
                status="degraded",
            ),
            AccountSnapshot(
                workspace_id=workspace.id,
                account_id=account.id,
                trigger="reconciliation",
                currency="USD",
                balance=Decimal("10000"),
                equity=Decimal("10000"),
                market_time=now,
                retrieved_at=now,
                source="oanda-v20",
            ),
            EconomicEvent(
                source="trading-economics",
                source_event_id=f"event-{uuid.uuid4()}",
                scheduled_at=now,
                timing_estimated=False,
                country="United States",
                currency="USD",
                category="Inflation",
                title="CPI",
                importance=3,
                retrieved_at=now,
            ),
            TradingViewAlert(
                workspace_id=workspace.id,
                account_id=account.id,
                external_event_id=f"alert-{uuid.uuid4()}",
                alert_name="test alert",
                symbol="XAUUSD",
                timeframe="5m",
                event_type="condition",
                market_time=now,
                received_at=now,
                close_price=Decimal("2400"),
                payload_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                verified_source_ip="52.89.214.238",
                verification_method="trusted_proxy_mtls_and_source_ip",
            ),
            TradingViewAlert(
                workspace_id=workspace.id,
                account_id=account.id,
                external_event_id=f"unverified-{uuid.uuid4()}",
                alert_name="manual fixture",
                symbol="XAUUSD",
                timeframe="5m",
                event_type="condition",
                market_time=now + timedelta(minutes=1),
                received_at=now + timedelta(minutes=1),
                payload_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                verified_source_ip="127.0.0.1",
                verification_method="manual-test",
            ),
        )
    )
    db_session.flush()

    reports = integration_verifications(
        Settings(
            _env_file=None,
            broker_provider="oanda",
            oanda_api_token="read-only-token",
            oanda_account_id="account-verified",
            news_provider="trading-economics",
            trading_economics_api_key="calendar-key",
            tradingview_webhook_enabled=True,
        ),
        db_session,
        scope=request_scope,
    )

    assert _report(reports, "oanda").reachability == "verified previously"
    assert _report(reports, "oanda").last_success_at == now
    assert _report(reports, "trading-economics").evidence == "observed"
    assert _report(reports, "tradingview").evidence == "observed"
    assert _report(reports, "tradingview").last_success_at == now


def test_live_verification_is_read_only_and_redacts_provider_failure(
    monkeypatch,
) -> None:
    secret = "never-print-this-provider-secret"

    class FailingConnector:
        async def account(self):
            raise RuntimeError(f"request failed with {secret}")

        async def positions(self):
            return ()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.services.integration_verification.create_oanda_connector",
        lambda settings: FailingConnector(),
    )
    report = IntegrationVerification(
        key="oanda",
        kind="broker",
        name="OANDA v20",
        implementation="implemented",
        configuration="configured",
        reachability="not tested",
        evidence="not observed",
        last_success_at=None,
        detail="configured",
    )

    result = asyncio.run(
        verify_live_integrations(
            Settings(
                _env_file=None,
                oanda_api_token=secret,
                oanda_account_id="account-1",
            ),
            (report,),
        )
    )[0]

    assert result.reachability == "unavailable"
    assert result.detail == "Read-only verification failed (RuntimeError)."
    assert result.verification_source == "real"
    assert secret not in result.detail


def test_live_oanda_verification_checks_account_without_persisting(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)

    class Connector:
        async def account(self):
            return AccountState(
                external_account_id="account-1",
                currency="USD",
                balance=Decimal("10000"),
                equity=Decimal("10000"),
                margin_used=Decimal("0"),
                margin_available=Decimal("10000"),
                market_time=now,
                retrieved_at=now,
                source="oanda-v20",
            )

        async def positions(self):
            return ()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.services.integration_verification.create_oanda_connector",
        lambda settings: Connector(),
    )
    report = IntegrationVerification(
        key="oanda",
        kind="broker",
        name="OANDA v20",
        implementation="implemented",
        configuration="configured",
        reachability="not tested",
        evidence="not observed",
        last_success_at=None,
        detail="configured",
    )

    result = asyncio.run(
        verify_live_integrations(
            Settings(
                _env_file=None,
                oanda_api_token="token",
                oanda_account_id="account-1",
            ),
            (report,),
        )
    )[0]

    assert result.reachability == "verified now"
    assert result.evidence == "observed"
    assert result.verification_source == "real"
    assert "0 open position(s)" in result.detail


def test_simulated_verification_is_never_reported_as_real() -> None:
    reports = tuple(
        IntegrationVerification(
            key=key,
            kind=kind,
            name=key,
            implementation="implemented",
            configuration="not configured",
            reachability="not tested",
            evidence="not observed",
            last_success_at=None,
            detail="not configured",
        )
        for key, kind in (
            ("oanda", "broker"),
            ("metatrader", "broker"),
            ("trading-economics", "news"),
            ("tradingview", "chart-alert"),
        )
    )

    verified = asyncio.run(verify_simulated_integrations(reports))

    assert {item.verification_source for item in verified} == {"simulated"}
    assert all(item.reachability == "verified now" for item in verified)
    assert all("not tested" in item.detail.lower() for item in verified)
