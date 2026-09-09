import uuid
from types import SimpleNamespace
from unittest.mock import Mock

from app.config import Settings
from app.costs import TokenUsage
from app.policy import PolicyEngine
from app.services import agent_gateway
from app.services.workspaces import RequestScope


class _FakeProvider:
    name = "ollama"
    model = "qwen3.5:9b"
    last_usage = TokenUsage(input_tokens=12, output_tokens=4)

    def __init__(self) -> None:
        self.tool_names: set[str] = set()
        self.mutation_error = ""
        self.instructions = ""

    def complete(self, *, tools, execute_tool, instructions, **_kwargs) -> str:
        self.tool_names = {tool["name"] for tool in tools}
        self.instructions = instructions
        try:
            execute_tool("create_trade_plan", {})
        except Exception as exc:
            self.mutation_error = str(exc)
        return "I can inspect the account, but that database change needs confirmation."


def test_pippy_turn_uses_full_agent_tools_and_denies_unconfirmed_mutation(
    monkeypatch,
) -> None:
    session_id = uuid.uuid4()
    scope = RequestScope(workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    conversation = SimpleNamespace(
        id=session_id,
        active_playbook_version_id=None,
    )
    user_turn = SimpleNamespace(id=uuid.uuid4())
    provider = _FakeProvider()
    written_turns: list[tuple[str, str]] = []

    monkeypatch.setattr(
        agent_gateway,
        "get_conversation",
        lambda *_args, **_kwargs: conversation,
    )
    monkeypatch.setattr(
        agent_gateway,
        "conversation_history",
        lambda *_args, **_kwargs: [],
    )

    def add_turn(_db, _conversation, role, content, **_kwargs):
        written_turns.append((role, content))
        return user_turn

    monkeypatch.setattr(agent_gateway, "add_turn", add_turn)
    monkeypatch.setattr(
        agent_gateway,
        "update_turn_outcome",
        lambda *_args, **_kwargs: user_turn,
    )
    monkeypatch.setattr(
        agent_gateway,
        "create_named_model_provider",
        lambda *_args, **_kwargs: provider,
    )

    class ReadyController:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def validate_selection(self, *_args, **_kwargs):
            return provider

        def close(self) -> None:
            pass

    monkeypatch.setattr(agent_gateway, "SessionModelController", ReadyController)

    result = agent_gateway.run_agent_turn(
        Mock(),
        engine=Mock(),
        settings=Settings(model_provider="ollama", resource_aware_model_routing=False),
        policy=PolicyEngine.load(),
        scope=scope,
        session_id=session_id,
        message="Review my trading context and record a plan.",
        provider_name="ollama",
        model="qwen3.5:9b",
    )

    assert "get_trade_context" in provider.tool_names
    assert "calculate_position_size" in provider.tool_names
    assert "create_trade_plan" in provider.tool_names
    assert provider.mutation_error == "trader declined mutation"
    assert "PIPPY VOICE INTERFACE" in provider.instructions
    assert "natural back-and-forth conversation" in provider.instructions
    assert [role for role, _content in written_turns] == ["user", "assistant"]
    assert result.provider == "ollama"
    assert result.model == "qwen3.5:9b"
    assert result.input_tokens == 12
    assert result.output_tokens == 4


def test_model_catalog_keeps_offline_local_configuration_visible(monkeypatch) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(
        agent_gateway,
        "create_model_provider",
        lambda *_args, **_kwargs: provider,
    )

    class OfflineController:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def options(self):
            return (SimpleNamespace(provider="ollama", model="qwen3.5:9b", local=True),)

        def validate_selection(self, *_args, **_kwargs):
            raise agent_gateway.ProviderConfigurationError("offline")

        def close(self) -> None:
            pass

    monkeypatch.setattr(agent_gateway, "SessionModelController", OfflineController)

    options = agent_gateway.selectable_agent_models(
        Settings(model_provider="ollama", ollama_model="qwen3.5:9b")
    )

    assert [(item.provider, item.model) for item in options] == [
        ("ollama", "qwen3.5:9b")
    ]
    assert options[0].available is False
    assert options[0].selected is True
