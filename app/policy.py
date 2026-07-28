import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.providers.base import ToolExecutor


class RuntimeRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    enforcement: str


class ToolPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    forbidden_names: frozenset[str] = Field(default_factory=frozenset)
    mutating_tools: frozenset[str] = Field(default_factory=frozenset)
    deterministic_tools: frozenset[str] = Field(default_factory=frozenset)
    max_tool_rounds: int = Field(default=5, ge=1, le=20)


class RuntimePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    rules: tuple[RuntimeRule, ...]
    tool_policy: ToolPolicy


class PolicyViolation(RuntimeError):
    pass


DIRECT_ACTION_METADATA = {
    "add_learning_module": {"mutating": True, "deterministic": False},
    "add_mindset_checkin": {"mutating": True, "deterministic": False},
    "add_strategy_test_sample": {"mutating": True, "deterministic": False},
    "add_trade_reflection": {"mutating": True, "deterministic": False},
    "analyze_chart": {"mutating": True, "deterministic": False},
    "build_edge_report": {"mutating": False, "deterministic": False},
    "calculate_broker_position_size": {"mutating": False, "deterministic": True},
    "calculate_position_size": {"mutating": False, "deterministic": True},
    "complete_strategy_experiment": {"mutating": True, "deterministic": False},
    "complete_pretrade_workflow": {"mutating": True, "deterministic": False},
    "configure_broker_connection": {"mutating": True, "deterministic": False},
    "rotate_broker_credential": {"mutating": True, "deterministic": False},
    "remove_broker_credential": {"mutating": True, "deterministic": False},
    "retry_broker_credential_cleanup": {"mutating": True, "deterministic": False},
    "create_api_principal": {"mutating": True, "deterministic": False},
    "grant_api_principal": {"mutating": True, "deterministic": False},
    "rotate_api_principal_token": {"mutating": True, "deterministic": False},
    "revoke_api_principal": {"mutating": True, "deterministic": False},
    "configure_tradingview_webhook": {"mutating": True, "deterministic": False},
    "configure_instrument_specification": {"mutating": True, "deterministic": True},
    "create_playbook_version": {"mutating": True, "deterministic": False},
    "create_strategy_experiment": {"mutating": True, "deterministic": False},
    "create_strategy_version": {"mutating": True, "deterministic": False},
    "create_trade_plan": {"mutating": True, "deterministic": False},
    "exclude_strategy_knowledge": {"mutating": True, "deterministic": False},
    "get_live_quote": {"mutating": False, "deterministic": False},
    "get_system_health": {"mutating": False, "deterministic": False},
    "get_recent_tradingview_alerts": {"mutating": False, "deterministic": False},
    "get_trade_plan": {"mutating": False, "deterministic": False},
    "verify_integrations": {"mutating": False, "deterministic": False},
    "ingest_tradingview_alert": {"mutating": True, "deterministic": False},
    "import_strategy_knowledge": {"mutating": True, "deterministic": False},
    "list_mindset_checkins": {"mutating": False, "deterministic": False},
    "list_trade_plans": {"mutating": False, "deterministic": False},
    "record_management_event": {"mutating": True, "deterministic": False},
    "restore_strategy_knowledge": {"mutating": True, "deterministic": False},
    "select_trading_account": {"mutating": True, "deterministic": False},
    "set_session_strategy": {"mutating": True, "deterministic": False},
    "clear_session_strategy": {"mutating": True, "deterministic": False},
    "set_learning_preferences": {"mutating": True, "deterministic": False},
    "synchronize_broker": {"mutating": True, "deterministic": False},
    "synchronize_news": {"mutating": True, "deterministic": False},
    "update_learning_progress": {"mutating": True, "deterministic": False},
}


@dataclass(frozen=True)
class ToolContext:
    name: str
    arguments: dict[str, Any]
    mutating: bool
    deterministic: bool


class PolicyEngine:
    def __init__(self, path: Path, policy: RuntimePolicy, content_hash: str) -> None:
        self.path = path
        self.policy = policy
        self.content_hash = content_hash

    @classmethod
    def load(cls, path: Path | None = None) -> "PolicyEngine":
        policy_path = path or Path(__file__).with_name("trading-rules.json")
        raw = policy_path.read_bytes()
        policy = RuntimePolicy.model_validate_json(raw)
        return cls(policy_path, policy, hashlib.sha256(raw).hexdigest())

    @property
    def version(self) -> str:
        return self.policy.version

    @property
    def short_hash(self) -> str:
        return self.content_hash[:12]

    @property
    def instructions(self) -> str:
        rendered = "\n".join(f"- [{rule.id}] {rule.text}" for rule in self.policy.rules)
        return f"Runtime policy {self.version} ({self.short_hash}):\n{rendered}"

    def assert_unchanged(self) -> None:
        current_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        if current_hash != self.content_hash:
            raise PolicyViolation(
                "Runtime policy changed after startup; restart before executing another tool"
            )

    def validate_tool_surface(
        self,
        tools: list[dict[str, Any]],
        metadata: dict[str, dict[str, bool]],
    ) -> None:
        names = {tool["name"] for tool in tools}
        forbidden = names & self.policy.tool_policy.forbidden_names
        if forbidden:
            raise PolicyViolation(f"Forbidden tools are exposed: {sorted(forbidden)}")
        if names != set(metadata):
            raise PolicyViolation("Every exposed tool must have explicit policy metadata")
        actual_mutating = {name for name, values in metadata.items() if values["mutating"]}
        if actual_mutating != set(self.policy.tool_policy.mutating_tools):
            raise PolicyViolation("Mutating tool metadata does not match runtime policy")
        actual_deterministic = {
            name for name, values in metadata.items() if values["deterministic"]
        }
        if actual_deterministic != set(self.policy.tool_policy.deterministic_tools):
            raise PolicyViolation("Deterministic tool metadata does not match runtime policy")

    def authorize(self, context: ToolContext) -> None:
        self.assert_unchanged()
        if context.name in self.policy.tool_policy.forbidden_names:
            raise PolicyViolation(f"Tool is forbidden by runtime policy: {context.name}")

    def authorize_registered_action(self, context: ToolContext) -> None:
        metadata = DIRECT_ACTION_METADATA.get(context.name)
        if metadata is None:
            raise PolicyViolation(
                f"Direct action has no explicit policy registration: {context.name}"
            )
        if (
            context.mutating != metadata["mutating"]
            or context.deterministic != metadata["deterministic"]
        ):
            raise PolicyViolation(
                f"Direct action metadata does not match registration: {context.name}"
            )
        self.authorize(context)


class ExecutionHooks:
    def __init__(
        self,
        policy: PolicyEngine,
        confirm_mutation: Any,
    ) -> None:
        self.policy = policy
        self.confirm_mutation = confirm_mutation

    def before_execute(self, context: ToolContext) -> None:
        self.policy.authorize(context)
        if context.mutating and not self.confirm_mutation(
            f"Policy-approved mutation: {context.name}",
            context.arguments,
        ):
            raise PolicyViolation("trader declined mutation")


def policy_wrapped_executor(
    execute: ToolExecutor,
    hooks: ExecutionHooks,
    metadata: dict[str, dict[str, bool]],
) -> ToolExecutor:
    def wrapped(name: str, arguments: dict[str, Any]) -> str:
        values = metadata.get(name)
        if values is None:
            raise PolicyViolation(f"Tool has no policy metadata: {name}")
        hooks.before_execute(
            ToolContext(
                name=name,
                arguments=arguments,
                mutating=values["mutating"],
                deterministic=values["deterministic"],
            )
        )
        return execute(name, arguments)

    return wrapped
