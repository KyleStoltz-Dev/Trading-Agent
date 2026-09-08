"""Small deterministic workflow cues for the natural-language trading session."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowCheckpoint:
    stage: str
    label: str
    instrument: str | None
    source_message: str

    def prompt_context(self) -> str:
        instrument = self.instrument or "not established"
        return (
            "CURRENT CONVERSATION WORKFLOW\n"
            f"Stage: {self.stage}\nInstrument: {instrument}\n"
            "Continue this workflow naturally. Gather available broker, market, news, "
            "strategy, and journal facts with read-only tools before asking the trader. "
            "Ask only for a missing human judgment that materially blocks the next step."
        )


_STAGES = {
    "reflect": "Reflect on the completed trade",
    "review": "Review recent trades",
    "manage": "Review an open position",
    "decide": "Evaluate a possible trade",
    "analyze": "Analyze market context",
    "prepare": "Prepare the trading session",
}
_DANGLING_COUNT_REQUEST = re.compile(
    r"^\s*(?:(?:(?:can|could|would)\s+you\s+)?"
    r"(?:show|give|list|review|get|pull)\s+(?:me\s+)?)?"
    r"(?P<fragment>(?:(?:the|my)\s+)?(?:last|latest|previous|recent)\s+\d+)"
    r"\s*(?:please)?[.!?]?\s*$",
    re.IGNORECASE,
)
_CLARIFICATION_CUES = (
    "clarif",
    "complete",
    "missing",
    "specif",
    "what exactly",
    "which ",
)


def dangling_count_fragment(message: str) -> str | None:
    match = _DANGLING_COUNT_REQUEST.fullmatch(message)
    return match.group("fragment").strip() if match is not None else None


def is_dangling_count_clarification(message: str, response: str) -> bool:
    """Identify unresolved count fragments so they stay out of future model history."""
    folded_response = response.casefold()
    return (
        dangling_count_fragment(message) is not None
        and "?" in response
        and any(cue in folded_response for cue in _CLARIFICATION_CUES)
    )


def normalize_instrument_symbol(value: str) -> str:
    """Normalize the common symbols traders use in conversation."""
    compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    aliases = {
        "BITCOIN": "BTC_USD",
        "BTC": "BTC_USD",
        "ETHEREUM": "ETH_USD",
        "ETH": "ETH_USD",
        "GOLD": "XAU_USD",
        "XAUUSD": "XAU_USD",
    }
    if compact in aliases:
        return aliases[compact]
    if len(compact) == 6 and compact.isalpha():
        return f"{compact[:3]}_{compact[3:]}"
    return value.strip().upper().replace("/", "_")


def _stage(message: str) -> str | None:
    if any(phrase in message for phrase in ("reflect", "lesson learned", "post-trade")):
        return "reflect"
    if any(phrase in message for phrase in ("manage", "open position", "move my stop")):
        return "manage"
    if any(
        phrase in message
        for phrase in ("recent trades", "trade history", "trading performance")
    ) or ("review" in message and "trade" in message):
        return "review"
    if any(
        phrase in message
        for phrase in (
            "preflight",
            "should i take",
            "possible entry",
            "trade decision",
            "take a trade",
            "taking a trade",
        )
    ):
        return "decide"
    if any(phrase in message for phrase in ("analyze", "analyse", "chart", "market context")):
        return "analyze"
    if any(
        phrase in message
        for phrase in ("prepare", "premarket", "pre-market", "session plan", "day-start")
    ):
        return "prepare"
    return None


def _instrument(message: str) -> str | None:
    match = re.search(r"\b([A-Z]{3})[_/]?([A-Z]{3})\b", message.upper())
    quote_currencies = {"USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD"}
    if match and match.group(2) in quote_currencies:
        return normalize_instrument_symbol(match.group(0))
    match = re.search(r"\b(BITCOIN|BTC|ETHEREUM|ETH|GOLD|XAUUSD)\b", message.upper())
    if not match:
        return None
    return normalize_instrument_symbol(match.group(1))


def infer_workflow_checkpoint(messages: list[str]) -> WorkflowCheckpoint | None:
    """Infer the most recent durable trading workflow cue from user-authored text."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        normalized = " ".join(message.casefold().split())
        stage = _stage(normalized)
        if stage is None:
            continue
        instrument = _instrument(message)
        if instrument is None:
            instrument = next(
                (
                    found
                    for earlier in reversed(messages[:index])
                    if (found := _instrument(earlier)) is not None
                ),
                None,
            )
        return WorkflowCheckpoint(
            stage=stage,
            label=_STAGES[stage],
            instrument=instrument,
            source_message=message,
        )
    return None


def should_refresh_trade_context(
    message: str,
    checkpoint: WorkflowCheckpoint | None,
) -> bool:
    """Refresh live evidence only for a new workflow cue or explicit continuation."""
    if checkpoint is None or checkpoint.instrument is None:
        return False
    if infer_workflow_checkpoint([message]) is not None:
        return True
    normalized = " ".join(message.casefold().split()).strip(" .!?")
    return normalized in {
        "continue",
        "continue this",
        "what is missing",
        "what's missing",
        "what changed",
        "recheck",
        "refresh",
    }
