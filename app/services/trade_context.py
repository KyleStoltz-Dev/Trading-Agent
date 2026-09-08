"""Assemble one bounded, read-only context pack for a trading decision."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import EvidenceItem, Observation, TradePlan
from app.services.event_relevance import instrument_event_currencies
from app.services.journal import get_trade_plan, list_trade_plans
from app.services.market_features import measure_candle_features
from app.services.pretrade import pretrade_alerts
from app.services.trading_workflow import normalize_instrument_symbol
from app.services.workspaces import RequestScope


async def _capture(
    label: str,
    operation: Callable[[], Any],
) -> tuple[str, Any, str | None]:
    try:
        return label, await operation(), None
    except Exception as exc:
        return label, None, type(exc).__name__


async def collect_broker_trade_context(
    connector: Any,
    *,
    instrument: str,
    timeframes: tuple[str, ...],
    candle_count: int,
) -> dict[str, Any]:
    """Collect independent broker reads without letting one outage erase the rest."""
    unique_timeframes = tuple(dict.fromkeys(item.upper() for item in timeframes if item))
    operations: list[tuple[str, Callable[[], Any]]] = [
        ("account", connector.account),
        ("positions", connector.positions),
        ("quote", lambda: connector.latest_quote(instrument)),
    ]
    operations.extend(
        (
            f"candles:{timeframe}",
            lambda timeframe=timeframe: connector.candles(
                instrument,
                timeframe,
                count=candle_count,
            ),
        )
        for timeframe in unique_timeframes
    )
    captured = await asyncio.gather(
        *(_capture(label, operation) for label, operation in operations)
    )
    result: dict[str, Any] = {
        "provider": getattr(connector, "name", None),
        "instrument": instrument,
        "account": None,
        "positions": [],
        "quote": None,
        "timeframes": {},
        "missing": [],
    }
    for label, value, error_type in captured:
        if error_type is not None:
            result["missing"].append({"read": label, "reason": error_type})
            continue
        if label == "account":
            result["account"] = {
                "currency": value.currency,
                "balance": value.balance,
                "equity": value.equity,
                "margin_used": value.margin_used,
                "margin_available": value.margin_available,
                "market_time": value.market_time,
                "retrieved_at": value.retrieved_at,
                "source": value.source,
            }
        elif label == "positions":
            result["positions"] = [
                {
                    "instrument": item.instrument,
                    "net_quantity": item.net_quantity,
                    "average_price": item.average_price,
                    "unrealized_pnl": item.unrealized_pnl,
                    "market_time": item.market_time,
                    "retrieved_at": item.retrieved_at,
                    "source": item.source,
                }
                for item in value
            ]
        elif label == "quote":
            result["quote"] = value
        else:
            timeframe = label.split(":", 1)[1]
            candles = list(value)
            features = None
            if candles:
                try:
                    features = measure_candle_features(candles)
                except ValueError as exc:
                    result["missing"].append(
                        {
                            "read": f"features:{timeframe}",
                            "reason": type(exc).__name__,
                        }
                    )
            result["timeframes"][timeframe] = {
                "candle_count": len(candles),
                "latest_candle": candles[-1] if candles else None,
                "features": features,
            }
    return result


async def collect_and_close_broker_trade_context(
    connector: Any,
    *,
    instrument: str,
    timeframes: tuple[str, ...],
    candle_count: int,
) -> dict[str, Any]:
    """Collect broker context and close its async transport on the same event loop."""
    try:
        return await collect_broker_trade_context(
            connector,
            instrument=instrument,
            timeframes=timeframes,
            candle_count=candle_count,
        )
    finally:
        await connector.aclose()


def _plan_summary(plan: TradePlan) -> dict[str, Any]:
    return {
        "reference": plan.reference,
        "instrument": plan.instrument,
        "direction": plan.direction,
        "strategy": plan.setup_name,
        "regime": plan.regime,
        "session": plan.session_name,
        "context_timeframe": plan.context_timeframe,
        "trigger_timeframe": plan.trigger_timeframe,
        "entry": plan.entry,
        "stop": plan.stop,
        "target": plan.target,
        "risk_percent": plan.risk_percent,
        "planned_r": plan.planned_r,
        "status": plan.status,
        "created_at": plan.created_at,
    }


def stored_trade_context(
    db: Session,
    *,
    scope: RequestScope,
    instrument: str,
    playbook_version_id: Any = None,
    trade_reference: str | None = None,
    now: datetime | None = None,
    news_window_minutes: int = 120,
    minimum_event_importance: int = 2,
) -> dict[str, Any]:
    """Return the active plan, linked charts, nearby news, and comparable plans."""
    symbol = normalize_instrument_symbol(instrument)
    plans = list_trade_plans(
        db,
        limit=6,
        scope=scope,
        playbook_version_id=playbook_version_id,
        instrument=symbol,
    )
    active_plan = None
    if trade_reference:
        active_plan = get_trade_plan(
            db,
            trade_reference,
            scope=scope,
            playbook_version_id=playbook_version_id,
        )
        if normalize_instrument_symbol(active_plan.instrument) != symbol:
            raise ValueError("trade plan instrument does not match the requested instrument")
    else:
        active_plan = next(
            (
                plan
                for plan in plans
                if plan.status in {"draft", "planned", "executed"}
            ),
            None,
        )

    compact_symbol = symbol.replace("_", "")
    chart_instrument = func.upper(
        func.replace(
            func.replace(EvidenceItem.metadata_json["instrument"].astext, "_", ""),
            "/",
            "",
        )
    )
    chart_match = chart_instrument == compact_symbol
    if active_plan is not None:
        chart_match = or_(EvidenceItem.trade_plan_id == active_plan.id, chart_match)
    evidence = list(
        db.scalars(
            select(EvidenceItem)
            .where(
                EvidenceItem.workspace_id == scope.workspace_id,
                EvidenceItem.account_id == scope.account_id,
                EvidenceItem.evidence_type == "chart",
                chart_match,
            )
            .order_by(EvidenceItem.retrieved_at.desc())
            .limit(8)
        )
    )
    evidence_ids = [item.id for item in evidence]
    feedback_by_evidence: dict[Any, list[str]] = {}
    if evidence_ids:
        feedback = list(
            db.scalars(
                select(Observation)
                .where(
                    Observation.workspace_id == scope.workspace_id,
                    Observation.account_id == scope.account_id,
                    Observation.evidence_id.in_(evidence_ids),
                    Observation.actor_type == "human",
                    Observation.kind == "confirmation",
                )
                .order_by(Observation.created_at.desc())
            )
        )
        for item in feedback:
            feedback_by_evidence.setdefault(item.evidence_id, []).append(item.text)
    charts = []
    for item in evidence:
        item_symbol = normalize_instrument_symbol(
            str((item.metadata_json or {}).get("instrument") or "")
        )
        linked_to_active = active_plan is not None and item.trade_plan_id == active_plan.id
        if not linked_to_active and item_symbol != symbol:
            continue
        charts.append(
            {
                "reference": f"evidence:{item.id}",
                "trade_reference": active_plan.reference if linked_to_active else None,
                "source": item.source,
                "instrument": (item.metadata_json or {}).get("instrument"),
                "venue": (item.metadata_json or {}).get("venue"),
                "stage": (item.metadata_json or {}).get("stage"),
                "timeframe": (item.metadata_json or {}).get("timeframe"),
                "market_time": item.market_time,
                "retrieved_at": item.retrieved_at,
                "trader_feedback": feedback_by_evidence.get(item.id, []),
            }
        )
        if len(charts) == 8:
            break

    current = now or datetime.now(UTC)
    alerts = pretrade_alerts(
        db,
        "trade",
        currencies=instrument_event_currencies(symbol),
        now=current,
        window_minutes=news_window_minutes,
        minimum_importance=minimum_event_importance,
    )
    return {
        "instrument": symbol,
        "active_plan": _plan_summary(active_plan) if active_plan is not None else None,
        "recent_comparable_plans": [
            _plan_summary(plan)
            for plan in plans
            if active_plan is None or plan.id != active_plan.id
        ][:5],
        "linked_charts": charts,
        "nearby_economic_events": alerts,
        "assembled_at": current,
    }
