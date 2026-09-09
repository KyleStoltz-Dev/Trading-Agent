"""Shared conversational gateway for terminal and voice interfaces.

The gateway keeps Pippy at the presentation edge. Model selection, tool exposure,
policy enforcement, audit records, and durable conversation history remain owned
by Trading Agent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.costs import TokenUsage
from app.models import ConversationSession
from app.policy import PolicyEngine
from app.providers import (
    ProviderConfigurationError,
    create_model_provider,
    create_named_model_provider,
)
from app.providers.base import valid_model_id
from app.services.agent import TradingAgent
from app.services.conversations import (
    add_turn,
    conversation_history,
    create_conversation,
    get_conversation,
    update_turn_outcome,
)
from app.services.model_selection import SessionModelController
from app.services.trading_workflow import is_dangling_count_clarification
from app.services.workspaces import RequestScope

AgentProvider = Literal["ollama", "openai", "anthropic"]
AgentMode = Literal["auto", "economy", "balanced", "deep"]

PIPPY_VOICE_INSTRUCTIONS = """
PIPPY VOICE INTERFACE
You are speaking aloud as Pippy, a capable personal AI assistant with access to Trading Agent's
tools and workflows. Hold a natural back-and-forth conversation instead of behaving like a command
line or returning a report. Lead with the useful answer, normally in one to three short spoken
sentences. Use contractions and varied sentence rhythm. Avoid markdown, headings, tables, raw JSON,
and long lists unless the user explicitly asks for detail. Ask at most one relevant follow-up
question. Do not narrate tool mechanics. Your name is Pippy, never Jarvis. All Trading Agent safety,
evidence, policy, confirmation, and order-execution restrictions above remain authoritative.
""".strip()


@dataclass(frozen=True, slots=True)
class AgentModelOption:
    provider: AgentProvider
    model: str
    label: str
    location: Literal["local", "cloud"]
    available: bool
    selected: bool


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    session_id: uuid.UUID
    response: str
    provider: str
    model: str
    mode: str
    input_tokens: int
    output_tokens: int
    references: tuple[dict[str, str | None], ...]


def selectable_agent_models(settings: Settings) -> tuple[AgentModelOption, ...]:
    """Return the same provider/model surface used by interactive chat.

    Provider discovery is best-effort. Configured models remain visible when a
    runtime is temporarily offline, but are marked unavailable.
    """

    initial_provider = create_model_provider(settings)
    controller = SessionModelController(settings, initial_provider)
    labels = {"ollama": "Local", "openai": "OpenAI", "anthropic": "Claude"}
    try:
        options: list[AgentModelOption] = []
        for option in controller.options():
            available = True
            try:
                controller.validate_selection(option.provider, option.model)
            except (ProviderConfigurationError, RuntimeError):
                available = False
            provider_label = labels[option.provider]
            access_mode = getattr(
                option,
                "access_mode",
                "local" if option.local else "api",
            )
            if access_mode == "subscription":
                provider_label = (
                    "ChatGPT subscription"
                    if option.provider == "openai"
                    else "Claude subscription"
                )
            elif access_mode == "api" and option.provider != "ollama":
                provider_label += " API"
            options.append(
                AgentModelOption(
                    provider=option.provider,  # type: ignore[arg-type]
                    model=option.model,
                    label=f"{provider_label} · {option.model}",
                    location="local" if option.local else "cloud",
                    available=available,
                    selected=(
                        option.provider == initial_provider.name
                        and option.model == initial_provider.model
                    ),
                )
            )
        return tuple(options)
    finally:
        controller.close()


def start_agent_session(
    db: Session,
    *,
    scope: RequestScope,
    name: str | None = None,
    title: str = "Pippy voice session",
) -> ConversationSession:
    return create_conversation(db, name=name, title=title, scope=scope)


def run_agent_turn(
    db: Session,
    *,
    engine: Engine,
    settings: Settings,
    policy: PolicyEngine,
    scope: RequestScope,
    session_id: uuid.UUID,
    message: str,
    provider_name: AgentProvider,
    model: str,
    mode: AgentMode = "auto",
) -> AgentTurnResult:
    """Run one durable Pippy turn through Trading Agent's complete tool surface.

    Voice-originated domain mutations are deliberately declined until the voice
    UI presents and confirms the exact generated tool arguments. Read-only tools,
    deterministic calculations, provider routing, policy checks, audit records,
    and PostgreSQL conversation logging are all active now.
    """

    if not valid_model_id(model):
        raise ValueError("model name contains unsupported characters")
    conversation = get_conversation(db, session_id, scope=scope)
    if conversation is None:
        raise LookupError("conversation session was not found")

    provider = create_named_model_provider(settings, provider_name)
    controller = SessionModelController(settings, provider)
    provider = controller.validate_selection(provider_name, model)
    request_id = uuid.uuid4()
    playbook_version_id = conversation.active_playbook_version_id
    user_turn = add_turn(
        db,
        conversation,
        "user",
        message,
        scope=scope,
        playbook_version_id=playbook_version_id,
        request_id=request_id,
        status="pending",
    )
    history = conversation_history(
        db,
        conversation,
        scope=scope,
        playbook_version_id=playbook_version_id,
        limit=settings.model_history_turn_limit,
    )

    # A model cannot authorize its own side effects. The follow-up milestone is a
    # two-phase, exact-arguments voice confirmation grant.
    def deny_unconfirmed_action(_action: str, _arguments: dict) -> bool:
        return False

    agent = TradingAgent(
        settings=settings,
        db=db,
        engine=engine,
        confirm_mutation=deny_unconfirmed_action,
        confirm_external_action=deny_unconfirmed_action,
        provider=provider,
        policy=policy,
        scope=scope,
        active_playbook_version_id=playbook_version_id,
    )
    agent.last_tool_audit = None
    try:
        prepared = agent.prepare(
            message,
            history,
            mode,
            model_override=model,
        )
        prepared = replace(
            prepared,
            instructions=f"{prepared.instructions}\n\n{PIPPY_VOICE_INSTRUCTIONS}",
        )
        response = agent.respond(
            message,
            history,
            mode=mode,
            prepared=prepared,
            request_id=request_id,
            conversation_session_id=conversation.id,
            user_turn_id=user_turn.id,
        )
    except Exception as exc:
        partial = bool(agent.last_tool_audit and agent.last_tool_audit.succeeded)
        outcome = "partial" if partial else "failed"
        update_turn_outcome(
            db,
            user_turn,
            scope=scope,
            status=outcome,
            error_type=type(exc).__name__,
        )
        add_turn(
            db,
            conversation,
            "assistant",
            (
                "The voice request stopped after a confirmed database change; "
                "the completed audit was retained."
                if partial
                else "The voice request failed before a complete response was produced."
            ),
            scope=scope,
            playbook_version_id=playbook_version_id,
            request_id=request_id,
            status=outcome,
            error_type=type(exc).__name__,
        )
        controller.close()
        raise

    clarification_only = is_dangling_count_clarification(message, response)
    outcome = "partial" if clarification_only else "complete"
    error_type = "ClarificationRequired" if clarification_only else None
    update_turn_outcome(
        db,
        user_turn,
        scope=scope,
        status=outcome,
        error_type=error_type,
    )
    add_turn(
        db,
        conversation,
        "assistant",
        response,
        scope=scope,
        playbook_version_id=playbook_version_id,
        request_id=request_id,
        status=outcome,
        error_type=error_type,
    )

    route = agent.last_route
    usage = getattr(provider, "last_usage", TokenUsage())
    references = tuple(
        {
            "kind": reference.kind,
            "label": reference.label,
            "locator": reference.locator,
            "retrieved_at": reference.retrieved_at,
        }
        for reference in agent.last_references
    )
    result = AgentTurnResult(
        session_id=conversation.id,
        response=response,
        provider=route.provider if route else provider.name,
        model=route.model if route else model,
        mode=route.mode if route else mode,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        references=references,
    )
    controller.close()
    return result
