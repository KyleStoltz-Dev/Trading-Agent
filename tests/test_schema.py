from alembic.script import ScriptDirectory

from app import models  # noqa: F401
from app.db import Base, alembic_config


def test_execution_centered_tables_are_declared() -> None:
    expected = {
        "account_snapshots",
        "account_constraint_profiles",
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
        "learning_curricula",
        "learning_modules",
        "market_contexts",
        "mindset_checkins",
        "news_items",
        "observations",
        "order_approvals",
        "order_intents",
        "playbook_versions",
        "playbooks",
        "position_snapshots",
        "pretrade_assessments",
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
    fill_columns = set(Base.metadata.tables["fills"].columns.keys())
    intent_columns = set(Base.metadata.tables["order_intents"].columns.keys())
    evidence_columns = set(Base.metadata.tables["evidence_items"].columns.keys())
    management_columns = set(Base.metadata.tables["trade_management_events"].columns.keys())
    conversation_turn_columns = set(Base.metadata.tables["conversation_turns"].columns.keys())
    mindset_columns = set(Base.metadata.tables["mindset_checkins"].columns.keys())
    curriculum_columns = set(Base.metadata.tables["learning_curricula"].columns.keys())
    learning_module_columns = set(Base.metadata.tables["learning_modules"].columns.keys())
    account_constraint_columns = set(
        Base.metadata.tables["account_constraint_profiles"].columns.keys()
    )
    pretrade_columns = set(Base.metadata.tables["pretrade_assessments"].columns.keys())

    assert {
        "external_event_id",
        "occurred_at",
        "ingested_at",
        "source_payload_hash",
    } <= event_columns
    assert {
        "commission",
        "financing",
        "guaranteed_execution_fee",
        "half_spread_cost",
    } <= fill_columns
    assert {"idempotency_key", "policy_hash", "expires_at"} <= intent_columns
    assert {"source", "market_time", "retrieved_at", "sha256"} <= evidence_columns
    assert {
        "event_type",
        "quantity_delta",
        "position_quantity_after",
        "realized_r_at_event",
        "reason",
    } <= management_columns
    assert "playbook_version_id" in conversation_turn_columns
    assert "playbook_version_id" in mindset_columns
    assert "emotional_state" in mindset_columns
    assert {
        "experience_level",
        "teaching_mode",
        "selected_topics",
        "source_tier_policy",
    } <= curriculum_columns
    assert {
        "module_key",
        "framework",
        "included",
        "objectives",
        "source_plan",
        "evidence_references",
        "learner_notes",
    } <= learning_module_columns
    assert {
        "account_type",
        "account_size",
        "currency",
        "firm_name",
        "program_name",
        "phase",
        "rule_limits",
        "active",
    } <= account_constraint_columns
    assert "account_constraint_profile_id" in pretrade_columns


def test_migration_history_has_one_head() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert len(script.get_heads()) == 1
