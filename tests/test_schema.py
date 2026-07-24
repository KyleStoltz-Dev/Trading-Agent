from alembic.script import ScriptDirectory

from app import models  # noqa: F401
from app.db import Base, alembic_config


def test_execution_centered_tables_are_declared() -> None:
    expected = {
        "account_snapshots",
        "analysis_runs",
        "broker_connections",
        "connector_cursors",
        "evidence_items",
        "economic_events",
        "execution_events",
        "fills",
        "instrument_mappings",
        "instrument_specifications",
        "instruments",
        "market_contexts",
        "mindset_checkins",
        "news_items",
        "observations",
        "order_approvals",
        "order_intents",
        "playbook_versions",
        "playbooks",
        "position_snapshots",
        "rule_evaluations",
        "trade_plans",
        "trade_reflections",
        "trade_management_events",
        "trades",
        "trading_accounts",
    }

    assert expected <= set(Base.metadata.tables)
    assert "ticks" not in Base.metadata.tables
    assert "quotes" not in Base.metadata.tables


def test_execution_records_have_provenance_and_idempotency_fields() -> None:
    event_columns = set(Base.metadata.tables["execution_events"].columns.keys())
    intent_columns = set(Base.metadata.tables["order_intents"].columns.keys())
    evidence_columns = set(Base.metadata.tables["evidence_items"].columns.keys())
    management_columns = set(
        Base.metadata.tables["trade_management_events"].columns.keys()
    )

    assert {
        "external_event_id",
        "occurred_at",
        "ingested_at",
        "source_payload_hash",
    } <= event_columns
    assert {"idempotency_key", "policy_hash", "expires_at"} <= intent_columns
    assert {"source", "market_time", "retrieved_at", "sha256"} <= evidence_columns
    assert {
        "event_type",
        "quantity_delta",
        "position_quantity_after",
        "realized_r_at_event",
        "reason",
    } <= management_columns


def test_migration_history_has_one_head() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert len(script.get_heads()) == 1
