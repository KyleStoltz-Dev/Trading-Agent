import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx


class WebSearchError(ValueError):
    pass


SENSITIVE_QUERY_PATTERNS = (
    re.compile(r"\b(?:api[_-]?key|password|secret|token)\s*[:=]", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b[A-Fa-f0-9]{64,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b"),
)


def validate_web_search_query(query: str) -> str:
    """Return the exact outbound query after fail-closed privacy validation."""
    normalized = " ".join(query.split())
    if not 3 <= len(normalized) <= 200:
        raise WebSearchError("web search query must contain 3 to 200 characters")
    if any(pattern.search(normalized) for pattern in SENSITIVE_QUERY_PATTERNS):
        raise WebSearchError(
            "web search query appears to contain private or credential-like data"
        )
    return normalized


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class WebSearchResponse:
    query: str
    retrieved_at: str
    provider: str
    results: tuple[WebSearchResult, ...]

    def model_dump(self) -> dict:
        return {
            "query": self.query,
            "retrieved_at": self.retrieved_at,
            "provider": self.provider,
            "results": [asdict(result) for result in self.results],
        }


def _safe_result_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return value


def search_brave(
    query: str,
    *,
    api_key: str,
    max_results: int = 5,
    timeout_seconds: float = 10,
    client: httpx.Client | None = None,
) -> WebSearchResponse:
    normalized = validate_web_search_query(query)
    if not api_key:
        raise WebSearchError("BRAVE_SEARCH_API_KEY is required for tier-3 web search")

    owns_client = client is None
    session = client or httpx.Client(
        timeout=timeout_seconds,
        trust_env=False,
        follow_redirects=False,
    )
    try:
        try:
            response = session.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key,
                },
                params={
                    "q": normalized,
                    "count": max_results,
                    "safesearch": "strict",
                    "text_decorations": "false",
                    "spellcheck": "true",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise WebSearchError(
                f"web search failed with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise WebSearchError("web search request failed") from exc
    finally:
        if owns_client:
            session.close()

    raw_results = (payload.get("web") or {}).get("results") or []
    results: list[WebSearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = _safe_result_url(item.get("url"))
        title = item.get("title")
        description = item.get("description")
        if url and isinstance(title, str) and isinstance(description, str):
            results.append(
                WebSearchResult(
                    title=" ".join(title.split())[:300],
                    url=url,
                    snippet=" ".join(description.split())[:1_000],
                )
            )
        if len(results) >= max_results:
            break
    return WebSearchResponse(
        query=normalized,
        retrieved_at=datetime.now(UTC).isoformat(),
        provider="brave",
        results=tuple(results),
    )
