import re
import unicodedata

UNSAFE_PROFILE_PATTERNS = (
    re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)", re.IGNORECASE),
    re.compile(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:[a-z0-9-]+\.)+"
        r"(?:com|net|org|io|ai|dev|co|uk|xyz|example)"
        r"(?:/[^\s]*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[^A-Za-z0-9])"
        r"(?:[A-Z0-9_]*(?:API_KEY|API_TOKEN|ACCESS_TOKEN|PASSWORD|SECRET|"
        r"ACCESS_KEY_ID))\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(
        r"(?:^|\s)(?:SYSTEM|DEVELOPER|ASSISTANT|TOOL)\s*:",
        re.IGNORECASE,
    ),
    re.compile(r"\[(?:SYSTEM|DEVELOPER|ASSISTANT|TOOL)\]", re.IGNORECASE),
    re.compile(r"<\|(?:system|developer|assistant|tool)\|>", re.IGNORECASE),
    re.compile(r"(?:\[INST\]|<<SYS>>)", re.IGNORECASE),
    re.compile(
        r"\b(?:ignore|disregard|override|bypass)\b.{0,40}"
        r"\b(?:instructions?|prompt|policy|safety|rules?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\b(?:developer|system)\s+(?:message|instructions?)\b", re.IGNORECASE),
    re.compile(r"\b(?:forget|disregard)\s+(?:the\s+)?instructions?\b", re.IGNORECASE),
    re.compile(r"\b(?:bypass|override)\s+(?:the\s+)?(?:policy|rules?)\b", re.IGNORECASE),
    re.compile(r"\breveal\s+(?:the\s+)?(?:secrets?|credentials?)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:reveal|show)\b.{0,30}"
        r"\b(?:system\s+prompt|developer\s+message|credentials?|secrets?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\byou\s+are\s+now\s+(?:(?:an?|the)\s+)?"
        r"(?:assistant|system(?:\s+assistant)?|developer)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:call|invoke|execute|run)\s+(?:a\s+)?"
        r"(?:tool|shell|sql|command)\b",
        re.IGNORECASE,
    ),
)
CREDENTIAL_PATTERNS = UNSAFE_PROFILE_PATTERNS[3:11]
TRADING_GOAL_TERMS = frozenset(
    {
        "accuracy",
        "backtest",
        "capital",
        "confidence",
        "confirmation",
        "consistency",
        "data",
        "decision",
        "discipline",
        "drawdown",
        "edge",
        "emotion",
        "entry",
        "execution",
        "exit",
        "focus",
        "forward",
        "fomo",
        "habit",
        "hesitation",
        "journal",
        "loss",
        "learn",
        "learning",
        "market",
        "mistake",
        "money",
        "news",
        "overtrading",
        "patience",
        "patient",
        "performance",
        "plan",
        "process",
        "protect",
        "profitability",
        "psychology",
        "quality",
        "review",
        "revenge",
        "risk",
        "rule",
        "session",
        "setup",
        "sizing",
        "strategy",
        "timing",
        "trade",
        "trading",
        "wait",
    }
)


def _profile_words(value: str) -> set[str]:
    return {match.group(0).casefold() for match in re.finditer(r"\b[A-Za-z][A-Za-z'-]*\b", value)}


def _contains_vocabulary(words: set[str], vocabulary: frozenset[str]) -> bool:
    if words & vocabulary:
        return True
    singulars = {word[:-1] for word in words if word.endswith("s") and len(word) > 3}
    if singulars & vocabulary:
        return True
    prefixes = (
        "trad",
        "profit",
        "consisten",
        "disciplin",
        "journal",
        "overtrad",
        "mistake",
    )
    return any(
        word.startswith(prefix) and any(term.startswith(prefix) for term in vocabulary)
        for word in words
        for prefix in prefixes
    )


def validate_profile_text(
    value: str,
    *,
    field_name: str,
    require_trading_goal: bool = False,
    allow_empty: bool = False,
    maximum_length: int | None = None,
) -> str:
    stripped = unicodedata.normalize("NFC", value).strip()
    if not stripped and not allow_empty:
        raise ValueError(f"{field_name} cannot be empty")
    if maximum_length is not None and len(stripped) > maximum_length:
        raise ValueError(f"{field_name} cannot exceed {maximum_length:,} characters")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value):
        raise ValueError(f"{field_name} cannot contain control or directionality characters")
    words = _profile_words(stripped)
    if any(pattern.search(stripped) for pattern in UNSAFE_PROFILE_PATTERNS):
        raise ValueError(
            f"{field_name} cannot contain URLs, credentials, or model-control instructions"
        )
    if require_trading_goal:
        has_goal_term = _contains_vocabulary(words, TRADING_GOAL_TERMS)
        if not has_goal_term:
            raise ValueError(
                f"{field_name} must describe a trading, risk, learning, or process goal"
            )
    return stripped


def validate_reflective_text(
    value: str,
    *,
    field_name: str,
    maximum_length: int = 2_000,
    maximum_lines: int = 40,
) -> str:
    """Validate journal prose as untrusted data without policing its tone."""
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    stripped = normalized.strip()
    if not stripped:
        raise ValueError(f"{field_name} cannot be empty")
    if len(stripped) > maximum_length:
        raise ValueError(f"{field_name} cannot exceed {maximum_length:,} characters")
    if stripped.count("\n") + 1 > maximum_lines:
        raise ValueError(f"{field_name} cannot exceed {maximum_lines} lines")
    if any(
        character != "\n" and unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in normalized
    ):
        raise ValueError(f"{field_name} cannot contain control or directionality characters")
    if any(pattern.search(stripped) for pattern in CREDENTIAL_PATTERNS):
        raise ValueError(f"{field_name} cannot contain credentials")
    return stripped
