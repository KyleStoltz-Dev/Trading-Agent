import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from app.connectors.metatrader_bridge import MetaTraderBridgeError
from app.connectors.oanda import OandaConnectorError
from app.connectors.trading_economics import TradingEconomicsError
from app.db import SessionLocal, upgrade_database
from app.integration_soak import run_soak
from app.market_data.contracts import AccountState, SyncPage
from app.models import ConnectorCursor, ExecutionEvent, Workspace
from app.services.broker_sync import (
    BrokerSyncInProgressError,
    synchronize_broker,
)
from app.services.catalog import configure_account
from app.services.integration_simulator import DeterministicIntegrationSimulator
from app.services.observability import JsonEventSink, event_json, structured_event
from app.services.workspaces import RequestScope


async def _no_sleep(_seconds: float) -> None:
    return None


def test_simulated_endpoints_cover_all_read_only_integrations() -> None:
    async def exercise():
        simulator = DeterministicIntegrationSimulator()
        try:
            oanda = simulator.oanda()
            mt = simulator.metatrader()
            news = simulator.trading_economics()
            return await asyncio.gather(
                oanda.account(),
                oanda.positions(),
                oanda.latest_quote("XAU_USD"),
                oanda.candles("XAU_USD", "M5", count=1),
                oanda.events_since("0"),
                mt.health(),
                mt.account(),
                mt.positions(),
                mt.latest_quote("XAUUSD"),
                mt.candles("XAUUSD", "M5", count=1),
                mt.events_since(None),
                news.news(limit=1),
                simulator.tradingview(
                    event_id="event-1",
                    secret=simulator.webhook_secret,
                ),
                simulator.tradingview(
                    event_id="event-1",
                    secret=simulator.webhook_secret,
                ),
            )
        finally:
            await simulator.aclose()

    results = asyncio.run(exercise())

    assert results[0].external_account_id == "sim-account-001"
    assert results[4].events[0].external_id == "1"
    assert results[5]["read_only"] is True
    assert results[11][0].external_id == "news-1"
    assert results[12].status_code == 202
    assert results[13].duplicate is True


def test_connectors_recover_from_timeout_reset_and_rate_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.connectors.oanda.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("app.connectors.metatrader_bridge.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("app.connectors.trading_economics.asyncio.sleep", _no_sleep)

    async def exercise():
        simulator = DeterministicIntegrationSimulator()
        summary = f"/v3/accounts/{simulator.account_id}/summary"
        simulator.inject("oanda", summary, ("timeout", "429"))
        simulator.inject("metatrader", "/v1/health", ("reset", "429"))
        simulator.inject("trading-economics", "/news", ("timeout", "429"))
        try:
            return await asyncio.gather(
                simulator.oanda().account(),
                simulator.metatrader().health(),
                simulator.trading_economics().news(limit=1),
            )
        finally:
            await simulator.aclose()

    account, health, news = asyncio.run(exercise())

    assert account.external_account_id == "sim-account-001"
    assert health["terminal_connected"] is True
    assert news[0].external_id == "news-1"


@pytest.mark.parametrize(
    ("connector_name", "provider", "path", "error"),
    (
        (
            "oanda",
            "oanda",
            "/v3/accounts/sim-account-001/summary",
            OandaConnectorError,
        ),
        (
            "metatrader",
            "metatrader",
            "/v1/account",
            MetaTraderBridgeError,
        ),
        (
            "trading_economics",
            "trading-economics",
            "/news",
            TradingEconomicsError,
        ),
    ),
)
def test_partial_pages_fail_closed(
    connector_name,
    provider,
    path,
    error,
) -> None:
    async def exercise():
        simulator = DeterministicIntegrationSimulator()
        simulator.inject(provider, path, ("partial-page",))
        try:
            connector = getattr(simulator, connector_name)()
            if connector_name == "oanda":
                await connector.account()
            elif connector_name == "metatrader":
                await connector.account()
            else:
                await connector.news(limit=1)
        finally:
            await simulator.aclose()

    with pytest.raises(error):
        asyncio.run(exercise())


def test_trading_economics_response_size_is_bounded() -> None:
    async def exercise():
        simulator = DeterministicIntegrationSimulator()
        try:
            connector = simulator.trading_economics()
            connector.maximum_response_bytes = 16
            await connector.news(limit=1)
        finally:
            await simulator.aclose()

    with pytest.raises(TradingEconomicsError, match="configured limit"):
        asyncio.run(exercise())


def test_simulated_oanda_runs_through_broker_sync(
    db_session,
    workspace_account,
) -> None:
    workspace, _ = workspace_account
    account, connection = configure_account(
        db_session,
        workspace_id=workspace.id,
        broker="OANDA",
        external_account_id="sim-account-001",
        label="Simulated OANDA",
        currency="USD",
        mode="practice",
        provider="oanda-v20",
        environment="practice",
        config_reference=None,
    )
    scope = RequestScope(workspace.id, account.id)
    db_session.add(
        ConnectorCursor(
            workspace_id=workspace.id,
            account_id=account.id,
            connection_id=connection.id,
            stream_name="transactions",
            cursor_value="0",
        )
    )
    db_session.commit()
    simulator = DeterministicIntegrationSimulator()
    try:
        result = asyncio.run(
            synchronize_broker(
                db_session,
                scope=scope,
                connection_id=connection.id,
                connector=simulator.oanda(),
            )
        )
    finally:
        asyncio.run(simulator.aclose())

    assert result.imported_events == 1
    assert result.imported_fills == 1
    event = db_session.scalar(
        select(ExecutionEvent).where(
            ExecutionEvent.workspace_id == workspace.id,
            ExecutionEvent.account_id == account.id,
            ExecutionEvent.external_event_id == "1",
        )
    )
    assert event is not None


def test_broker_sync_advisory_lock_rejects_concurrent_stress() -> None:
    try:
        upgrade_database()
    except OperationalError:
        pytest.skip("PostgreSQL is unavailable")
    workspace_id = uuid.uuid4()
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingConnector:
        name = "stress-broker"

        async def events_since(self, cursor):
            started.set()
            await release.wait()
            return SyncPage(
                events=(),
                cursor_before=cursor,
                cursor_after="0",
                has_more=False,
                coverage="complete",
            )

        async def account(self):
            now = datetime.now(UTC)
            return AccountState(
                external_account_id="stress-account",
                currency="USD",
                balance=Decimal("10000"),
                equity=Decimal("10000"),
                margin_used=Decimal("0"),
                margin_available=Decimal("10000"),
                market_time=now,
                retrieved_at=now,
                source=self.name,
            )

        async def positions(self):
            return ()

    with SessionLocal() as setup:
        workspace = Workspace(
            id=workspace_id,
            slug=f"stress-{uuid.uuid4().hex}",
            name="Sync stress",
        )
        setup.add(workspace)
        setup.commit()
        account, connection = configure_account(
            setup,
            workspace_id=workspace.id,
            broker="stress",
            external_account_id="stress-account",
            label="Stress",
            currency="USD",
            mode="practice",
            provider="stress-broker",
            environment="practice",
            config_reference=None,
        )
        scope = RequestScope(workspace.id, account.id)
        connection_id = connection.id

    async def first():
        with SessionLocal() as session:
            return await synchronize_broker(
                session,
                scope=scope,
                connection_id=connection_id,
                connector=BlockingConnector(),
            )

    async def contender():
        with SessionLocal() as session:
            with pytest.raises(BrokerSyncInProgressError):
                await synchronize_broker(
                    session,
                    scope=scope,
                    connection_id=connection_id,
                    connector=BlockingConnector(),
                )

    async def stress():
        owner = asyncio.create_task(first())
        await started.wait()
        await asyncio.gather(*(contender() for _ in range(20)))
        release.set()
        await owner

    try:
        asyncio.run(stress())
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(
                delete(Workspace).where(Workspace.id == workspace_id)
            )
            cleanup.commit()


def test_soak_runner_is_bounded_and_emits_redacted_json() -> None:
    lines: list[str] = []
    sink = JsonEventSink(lines.append)

    result = asyncio.run(
        run_soak(
            iterations=3,
            max_seconds=5,
            fault_every=2,
            sink=sink,
        )
    )

    assert result.completed_iterations == 3
    assert result.checks == 42
    assert result.failures == 0
    assert result.verification_source == "simulated"
    assert all(json.loads(line)["schema"].startswith("trading-agent") for line in lines)

    event = structured_event(
        "integration.test",
        component="test",
        outcome="failed",
        fields={
            "Authorization": "Bearer secret-value-that-must-not-leak",
            "detail": "token=abcdefghijklmnopqrstuvwxyz123456",
        },
    )
    rendered = event_json(event)
    assert "secret-value-that-must-not-leak" not in rendered
    assert "abcdefghijklmnopqrstuvwxyz123456" not in rendered
