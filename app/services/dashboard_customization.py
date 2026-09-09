import json
from typing import Any

from pydantic import ValidationError

from app.providers.base import ModelProvider
from app.schemas import DashboardCustomizeRequest, DashboardLayoutSpec


class DashboardCustomizationError(RuntimeError):
    pass


SYSTEM_PROMPT = """You customize a Trading-Agent dashboard using a strict layout schema.
Return JSON only, with two keys: spec and summary. Never include markdown or prose outside JSON.
You may change only presentation: name, accent, density, width, cardStyle, widget visibility,
and sectionOrder. Preserve every supported widget key and every section exactly once. Never
invent, alter, estimate, or discuss account values, prices, P&L, news, trades, risk limits,
credentials, or broker state. Never generate HTML, JavaScript, URLs, or executable code.
Valid accents: teal, blue, violet, gold. Valid density: comfortable, compact. Valid width:
wide, focused. Valid cardStyle: rounded, square. Valid sections: metrics, market, workflow,
context. Keep summary under 300 characters and describe only visible layout changes."""


def _unavailable_tool(_name: str, _arguments: dict[str, Any]) -> str:
    raise DashboardCustomizationError("dashboard customization exposes no tools")


def customize_dashboard_layout(
    provider: ModelProvider,
    request: DashboardCustomizeRequest,
) -> tuple[DashboardLayoutSpec, str]:
    message = json.dumps(
        {
            "request": request.request,
            "current": request.current.model_dump(mode="json", by_alias=True),
        },
        separators=(",", ":"),
    )
    raw = provider.complete(
        instructions=SYSTEM_PROMPT,
        message=message,
        history=[],
        tools=[],
        execute_tool=_unavailable_tool,
        max_tool_rounds=1,
        reasoning_effort="low",
        max_output_tokens=1200,
    )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"spec", "summary"}:
            raise ValueError("response must contain only spec and summary")
        spec = DashboardLayoutSpec.model_validate(payload["spec"])
        summary = str(payload["summary"]).strip()
        if not summary or len(summary) > 300:
            raise ValueError("summary must contain between 1 and 300 characters")
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise DashboardCustomizationError(
            "model returned an invalid dashboard layout; no changes were applied"
        ) from exc
    return spec, summary
