import re
from dataclasses import dataclass
from typing import Literal

from app.config import Settings

AgentMode = Literal["auto", "economy", "balanced", "deep"]
TaskClass = Literal["routine", "analysis", "deep_research"]


@dataclass(frozen=True)
class ModelRoute:
    mode: AgentMode
    task_class: TaskClass
    provider: str
    model: str
    reasoning_effort: Literal["low", "medium", "high"]
    reason: str


DEEP_PATTERNS = (
    r"\bdeep(?:ly)?\b",
    r"\bresearch\b",
    r"\bbacktest\b",
    r"\bcompare\b.*\b(?:regime|period|year|setup)",
    r"\bfind (?:my )?edge\b",
    r"\bconflict(?:ing)? rules?\b",
    r"\bfull history\b",
    r"\blarge (?:sample|dataset|export)\b",
)
ROUTINE_PATTERNS = (
    r"\b(?:log|journal|record|save|import)\b",
    r"\bsummarize (?:this|these)\b",
    r"\blist (?:my )?(?:trades|plans|sessions)\b",
    r"\bhealth\b",
)


def classify_task(message: str) -> TaskClass:
    normalized = " ".join(message.lower().split())
    if any(re.search(pattern, normalized) for pattern in DEEP_PATTERNS):
        return "deep_research"
    if any(re.search(pattern, normalized) for pattern in ROUTINE_PATTERNS):
        return "routine"
    return "analysis"


def route_model(
    settings: Settings,
    provider: str,
    message: str,
    mode: AgentMode | None = None,
) -> ModelRoute:
    selected_mode = mode or settings.agent_mode
    task_class = classify_task(message)
    resolved_mode: AgentMode
    if selected_mode == "auto":
        resolved_mode = {
            "routine": "economy",
            "analysis": "balanced",
            "deep_research": "deep",
        }[task_class]
        reason = f"automatic route for {task_class.replace('_', ' ')}"
    else:
        resolved_mode = selected_mode
        reason = f"user-selected {selected_mode} mode"

    prefix = "openai" if provider == "openai" else "anthropic"
    fallback = getattr(settings, f"{prefix}_model")
    model = getattr(settings, f"{prefix}_{resolved_mode}_model") or fallback
    effort = {"economy": "low", "balanced": "medium", "deep": "high"}[resolved_mode]
    return ModelRoute(
        mode=resolved_mode,
        task_class=task_class,
        provider=provider,
        model=model,
        reasoning_effort=effort,
        reason=reason,
    )
