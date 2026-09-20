"""Cross-interface journeys with real persistence and controlled external providers."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

import app.cli as cli
from app.config import Settings
from app.models import ConversationTurn
from app.policy import PolicyEngine, PolicyViolation
from app.services import agent_gateway, conversation_context, request_lifecycle
from app.services.conversations import (
    add_turn,
    conversation_workflow,
    create_conversation,
    get_conversation,
    update_turn_outcome,
)
from app.services.trading_workflow import advance_workflow
from app.services.workspaces import RequestScope


def _turn(db, conversation, scope, message, **kwargs):
    return add_turn(
        db, conversation, "user", message, scope=scope,
        playbook_version_id=conversation.active_playbook_version_id, **kwargs,
    )


def test_journey_survives_cancellation_restart_and_short_model_history(db_session, request_scope):
    conversation = create_conversation(db_session, title="Gold journey", scope=request_scope)
    _turn(db_session, conversation, request_scope, "Prepare my XAUUSD session")
    for _ in range(30):
        _turn(db_session, conversation, request_scope, "Thanks")
    cancelled = _turn(
        db_session, conversation, request_scope, "Analyze the chart", status="pending",
    )
    update_turn_outcome(
        db_session, cancelled, scope=request_scope, status="failed", error_type="UserCancelled",
    )
    session_id = conversation.id
    db_session.expire_all()
    resumed = get_conversation(db_session, session_id, scope=request_scope)
    checkpoint = conversation_workflow(db_session, resumed, scope=request_scope, history=[])
    assert (checkpoint.stage, checkpoint.instrument) == ("analyze", "XAU_USD")
    _turn(db_session, resumed, request_scope, "pause")
    assert conversation_workflow(db_session, resumed, scope=request_scope).status == "paused"
    _turn(db_session, resumed, request_scope, "continue")
    assert conversation_workflow(db_session, resumed, scope=request_scope).stage == "analyze"
    assert conversation_workflow(db_session, resumed, scope=request_scope).status == "active"
    _turn(db_session, resumed, request_scope, "skip this trade")
    assert conversation_workflow(db_session, resumed, scope=request_scope).stage == "no_trade"


def test_checkpoint_never_crosses_accounts_or_conversations(db_session, request_scope):
    conversation = create_conversation(db_session, title="Gold", scope=request_scope)
    _turn(db_session, conversation, request_scope, "Analyze gold")
    other = create_conversation(db_session, title="Other", scope=request_scope)
    assert conversation_workflow(db_session, other, scope=request_scope) is None
    with pytest.raises(LookupError):
        conversation_workflow(
            db_session, conversation, scope=RequestScope(request_scope.workspace_id, uuid.uuid4()),
        )


def test_pausing_and_no_trade_never_trigger_broker_reads(db_session, request_scope, monkeypatch):
    conversation = create_conversation(db_session, title="No trade", scope=request_scope)
    _turn(db_session, conversation, request_scope, "Analyze gold")
    reads = Mock()
    monkeypatch.setattr(conversation_context, "assemble_trade_context", reads)
    for message in ("pause", "skip this trade"):
        _turn(db_session, conversation, request_scope, message)
        _, _, checkpoint = conversation_context.prepare_turn_context(
            db_session, Settings(), conversation, message, [],
            scope=request_scope, policy=PolicyEngine.load(),
        )
        assert checkpoint is not None
    reads.assert_not_called()


def test_shared_context_recovers_from_provider_failure_but_not_policy_failure(
    db_session, request_scope, monkeypatch,
):
    conversation = create_conversation(db_session, title="Recovery", scope=request_scope)
    _turn(db_session, conversation, request_scope, "Analyze XAUUSD")
    reads = Mock(side_effect=TimeoutError("provider is unavailable"))
    monkeypatch.setattr(conversation_context, "assemble_trade_context", reads)
    args = (db_session, Settings(), conversation, "continue", [])
    context, references, checkpoint = conversation_context.prepare_turn_context(
        *args, scope=request_scope, policy=PolicyEngine.load(),
    )
    assert "RETRIEVAL STATUS" in context
    assert "CURRENT READ-ONLY TRADE CONTEXT" not in context
    assert checkpoint.instrument == "XAU_USD"
    assert references == []
    reads.side_effect = None
    reads.return_value = ("Fresh timestamped evidence", [])
    context, _, _ = conversation_context.prepare_turn_context(
        *args, scope=request_scope, policy=PolicyEngine.load(),
    )
    assert "Fresh timestamped evidence" in context
    policy = Mock()
    policy.authorize_registered_action.side_effect = PolicyViolation("policy changed")
    reads.reset_mock()
    with pytest.raises(PolicyViolation):
        conversation_context.prepare_turn_context(*args, scope=request_scope, policy=policy)
    reads.assert_not_called()


@pytest.mark.parametrize("failure", [TimeoutError, KeyboardInterrupt, asyncio.CancelledError])
def test_gateway_failure_retains_checkpoint_closes_provider_and_can_resume(
    db_session, request_scope, monkeypatch, failure,
):
    conversation = create_conversation(db_session, title="Resume", scope=request_scope)
    provider = Mock(name="provider")
    provider.name = "ollama"
    controller = Mock()
    controller.validate_selection.return_value = provider
    monkeypatch.setattr(agent_gateway, "create_named_model_provider", Mock(return_value=provider))
    monkeypatch.setattr(agent_gateway, "SessionModelController", Mock(return_value=controller))
    agent = Mock(last_tool_audit=None)
    agent.respond.side_effect = failure("interrupted")
    # A prepared dataclass is needed by the interface-instruction replace step.
    from app.routing import ModelRoute
    from app.services.agent import PreparedAgentRequest
    agent.prepare.return_value = PreparedAgentRequest(
        route=ModelRoute("balanced", "routine", "ollama", "qwen3.5:9b", "medium", "test"),
        instructions="test", message="Analyze XAUUSD", history=[],
    )
    monkeypatch.setattr(agent_gateway, "TradingAgent", Mock(return_value=agent))
    evidence = Mock(return_value=("Shared broker evidence", []))
    monkeypatch.setattr(conversation_context, "assemble_trade_context", evidence)
    with pytest.raises(failure):
        agent_gateway.run_agent_turn(
            db_session, engine=Mock(), settings=Settings(), policy=PolicyEngine.load(),
            scope=request_scope, session_id=conversation.id, message="Analyze XAUUSD",
            provider_name="ollama", model="qwen3.5:9b",
        )
    controller.close.assert_called_once()
    assert "Shared broker evidence" in agent.prepare.call_args.kwargs["evidence_context"]
    turns = list(db_session.scalars(select(ConversationTurn).where(
        ConversationTurn.session_id == conversation.id,
    )))
    assert len(turns) == 2
    assert all(turn.status == "failed" for turn in turns)
    assert next(turn for turn in turns if turn.role == "user").workflow_checkpoint
    resumed = conversation_workflow(db_session, conversation, scope=request_scope, history=[])
    assert resumed.instrument == "XAU_USD"
    assert cli.prepare_turn_context is agent_gateway.prepare_turn_context


def test_completed_mutation_is_not_claimed_rolled_back(db_session, request_scope):
    conversation = create_conversation(db_session, title="Partial", scope=request_scope)
    request_id = uuid.uuid4()
    user_turn = _turn(
        db_session, conversation, request_scope, "Review my trades", request_id=request_id,
        status="pending",
    )
    partial = request_lifecycle.record_request_failure(
        db_session, agent=SimpleNamespace(last_tool_audit=SimpleNamespace(succeeded=True)),
        user_turn=user_turn, conversation=conversation, scope=request_scope,
        playbook_version_id=None, request_id=request_id, error_type="UserCancelled",
    )
    assert partial
    assert user_turn.status == "partial"


def test_symbol_change_is_not_lost_on_continuation():
    gold = advance_workflow(None, "Analyze XAUUSD")
    eur = advance_workflow(gold, "EURUSD instead")
    assert advance_workflow(eur, "continue").instrument == "EUR_USD"


def test_request_intent_is_frozen_when_another_interface_posts_a_later_turn(
    db_session, request_scope, monkeypatch,
):
    conversation = create_conversation(db_session, title="Concurrent", scope=request_scope)
    _turn(db_session, conversation, request_scope, "Analyze gold")
    first = _turn(db_session, conversation, request_scope, "continue", status="pending")
    _turn(db_session, conversation, request_scope, "Analyze EURUSD", status="pending")
    reads = Mock(return_value=("Evidence", []))
    monkeypatch.setattr(conversation_context, "assemble_trade_context", reads)
    _, _, checkpoint = conversation_context.prepare_turn_context(
        db_session, Settings(), conversation, "continue", [],
        scope=request_scope, policy=PolicyEngine.load(), user_turn=first,
    )
    assert checkpoint.instrument == "XAU_USD"
    assert reads.call_args.args[3].instrument == "XAU_USD"


@pytest.mark.parametrize("message,stage", [
    ("Prepare my session", "prepare"), ("Analyze my chart", "analyze"),
    ("Should I take this trade?", "decide"), ("Review my open position", "manage"),
    ("Post-trade reflection", "reflect"), ("Review recent trades", "review"),
    ("skip this trade", "no_trade"),
])
def test_each_stage_is_durable_without_claiming_execution(
    db_session, request_scope, message, stage,
):
    conversation = create_conversation(db_session, title="Lifecycle", scope=request_scope)
    _turn(db_session, conversation, request_scope, "Analyze XAUUSD")
    turn = _turn(db_session, conversation, request_scope, message)
    db_session.expire_all()
    checkpoint = conversation_workflow(db_session, conversation, scope=request_scope, history=[])
    assert checkpoint.stage == stage
    assert checkpoint.instrument == "XAU_USD"
    assert "approved" not in turn.workflow_checkpoint
    assert "order_id" not in turn.workflow_checkpoint


def test_declarative_catalog_is_the_runtime_policy_surface():
    from app.services import agent, agent_tools
    assert agent.TOOLS is agent_tools.TOOLS
    assert agent.TOOL_METADATA is agent_tools.TOOL_METADATA
    PolicyEngine.load().validate_tool_surface(agent_tools.TOOLS, agent_tools.TOOL_METADATA)
