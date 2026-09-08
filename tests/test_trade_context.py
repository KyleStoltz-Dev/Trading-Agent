import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import app.cli as cli_module
from app.market_data.contracts import AccountState, Candle, PositionState
from app.services import trade_context as trade_context_module
from app.services.agent import TOOLS
from app.services.evidence import record_chart_feedback
from app.services.trade_context import (
    collect_and_close_broker_trade_context,
    collect_broker_trade_context,
    stored_trade_context,
)
from app.services.trading_workflow import normalize_instrument_symbol
from app.services.workspaces import RequestScope


def _candles(timeframe: str) -> tuple[Candle, ...]:
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    return tuple(
        Candle(
            instrument="XAU_USD",
            timeframe=timeframe,
            started_at=now + timedelta(minutes=index * 5),
            open=Decimal("3500") + index,
            high=Decimal("3502") + index,
            low=Decimal("3499") + index,
            close=Decimal("3501") + index,
            volume=Decimal("100"),
            complete=True,
            retrieved_at=now,
            source="test",
            venue="practice",
        )
        for index in range(3)
    )


class PartialBroker:
    name = "test-broker"

    async def account(self):
        now = datetime(2026, 9, 2, 12, tzinfo=UTC)
        return AccountState(
            external_account_id="hidden",
            currency="USD",
            balance=Decimal("100000"),
            equity=Decimal("100100"),
            margin_used=Decimal("0"),
            margin_available=Decimal("100100"),
            market_time=now,
            retrieved_at=now,
            source=self.name,
        )

    async def positions(self):
        now = datetime(2026, 9, 2, 12, tzinfo=UTC)
        return (
            PositionState(
                external_id="broker-position-secret",
                instrument="XAU_USD",
                net_quantity=Decimal("1"),
                average_price=Decimal("3500"),
                unrealized_pnl=Decimal("10"),
                market_time=now,
                retrieved_at=now,
                source=self.name,
            ),
        )

    async def latest_quote(self, _instrument):
        raise TimeoutError("temporary quote outage")

    async def candles(self, _instrument, timeframe, *, count):
        assert count == 20
        return _candles(timeframe)


def test_collect_broker_context_preserves_partial_evidence() -> None:
    result = asyncio.run(
        collect_broker_trade_context(
            PartialBroker(),
            instrument="XAU_USD",
            timeframes=("H4", "M5", "M5"),
            candle_count=20,
        )
    )

    assert result["account"]["equity"] == Decimal("100100")
    assert "external_account_id" not in result["account"]
    assert "external_id" not in result["positions"][0]
    assert result["quote"] is None
    assert set(result["timeframes"]) == {"H4", "M5"}
    assert result["timeframes"]["H4"]["features"]["candle_count"] == 3
    assert result["missing"] == [{"read": "quote", "reason": "TimeoutError"}]


def test_stored_context_selects_latest_open_plan_and_normalizes_symbol(
    monkeypatch,
) -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    active = SimpleNamespace(
        id=uuid.uuid4(),
        reference="xau-usd-20260902-ny-long-1",
        instrument="XAUUSD",
        direction="long",
        setup_name="kyle-price-action",
        regime="bearish countertrend",
        session_name="New York",
        context_timeframe="H4",
        trigger_timeframe="M5",
        entry=Decimal("3500"),
        stop=Decimal("3490"),
        target=Decimal("3530"),
        risk_percent=Decimal("0.5"),
        planned_r=Decimal("3"),
        status="planned",
        created_at=now,
    )
    monkeypatch.setattr(
        trade_context_module,
        "list_trade_plans",
        Mock(return_value=[active]),
    )
    monkeypatch.setattr(trade_context_module, "pretrade_alerts", Mock(return_value=[]))
    database = Mock()
    database.scalars.return_value = []
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())

    result = stored_trade_context(
        database,
        scope=scope,
        instrument="gold",
        now=now,
    )

    assert result["instrument"] == "XAU_USD"
    assert result["active_plan"]["reference"] == active.reference
    assert result["linked_charts"] == []
    trade_context_module.list_trade_plans.assert_called_once_with(
        database,
        limit=6,
        scope=scope,
        playbook_version_id=None,
        instrument="XAU_USD",
    )
    trade_context_module.pretrade_alerts.assert_called_once_with(
        database,
        "trade",
        currencies=frozenset({"USD"}),
        now=now,
        window_minutes=120,
        minimum_importance=2,
    )


def test_context_uses_configured_news_gate(monkeypatch) -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    monkeypatch.setattr(trade_context_module, "list_trade_plans", Mock(return_value=[]))
    alerts = Mock(return_value=[])
    monkeypatch.setattr(trade_context_module, "pretrade_alerts", alerts)
    database = Mock()
    database.scalars.return_value = []
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())

    stored_trade_context(
        database,
        scope=scope,
        instrument="XAUUSD",
        now=now,
        news_window_minutes=480,
        minimum_event_importance=3,
    )

    assert alerts.call_args.kwargs["window_minutes"] == 480
    assert alerts.call_args.kwargs["minimum_importance"] == 3


def test_context_closes_broker_on_the_collection_event_loop() -> None:
    class LoopBoundBroker(PartialBroker):
        collection_loop = None
        closed_loop = None

        async def account(self):
            self.collection_loop = asyncio.get_running_loop()
            return await super().account()

        async def aclose(self):
            self.closed_loop = asyncio.get_running_loop()

    broker = LoopBoundBroker()
    asyncio.run(
        collect_and_close_broker_trade_context(
            broker,
            instrument="XAU_USD",
            timeframes=("H4", "M5"),
            candle_count=20,
        )
    )

    assert broker.closed_loop is broker.collection_loop


def test_trade_context_tool_is_a_single_read_only_surface() -> None:
    tool = next(item for item in TOOLS if item["name"] == "get_trade_context")

    assert tool["strict"] is True
    assert set(tool["parameters"]["required"]) == {
        "instrument",
        "context_timeframe",
        "trigger_timeframe",
        "candle_count",
        "trade_reference",
    }


def test_chart_feedback_is_scoped_to_exact_evidence() -> None:
    evidence = SimpleNamespace(id=uuid.uuid4(), trade_plan_id=uuid.uuid4())
    database = Mock()
    database.scalar.return_value = evidence
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())

    feedback = record_chart_feedback(
        database,
        scope=scope,
        evidence_reference=f"evidence:{evidence.id}",
        category="wrong_phase",
        feedback="This is reaccumulation, not distribution.",
    )

    assert feedback.evidence_id == evidence.id
    assert feedback.trade_plan_id == evidence.trade_plan_id
    assert feedback.actor_type == "human"
    assert feedback.text == "wrong_phase: This is reaccumulation, not distribution."
    database.commit.assert_called_once()


def test_instrument_normalization_matches_conversation_symbols() -> None:
    assert normalize_instrument_symbol("EUR/USD") == "EUR_USD"
    assert normalize_instrument_symbol("xauusd") == "XAU_USD"
    assert normalize_instrument_symbol("bitcoin") == "BTC_USD"


def test_chat_context_is_assembled_without_a_configured_broker(monkeypatch) -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    monkeypatch.setattr(
        cli_module,
        "stored_trade_context",
        Mock(
            return_value={
                "instrument": "XAU_USD",
                "active_plan": None,
                "recent_comparable_plans": [],
                "linked_charts": [],
                "nearby_economic_events": [],
                "assembled_at": now,
            }
        ),
    )
    conversation = SimpleNamespace(active_playbook_version_id=None)
    checkpoint = SimpleNamespace(instrument="XAU_USD")
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())

    context, references = cli_module._automatic_chat_trade_context(
        Mock(),
        cli_module.Settings(broker_provider="none"),
        conversation,
        checkpoint,
        scope=scope,
    )

    assert "CURRENT READ-ONLY TRADE CONTEXT" in context
    assert '"reason":"not_configured"' in context
    assert references == []
