"""OpenAI Realtime session adapter used by the local Pippy gateway."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.providers.base import ProviderConfigurationError, valid_model_id

REALTIME_SESSION_URL = "https://api.openai.com/v1/realtime/client_secrets"
SUPPORTED_VOICES = frozenset({"marin", "cedar"})

PIPPY_REALTIME_INSTRUCTIONS = (
    "You are Pippy, a warm, concise, voice-first personal AI assistant. Hold a natural "
    "back-and-forth conversation and allow the user to interrupt you. For anything involving "
    "markets, trading evidence, account context, strategies, risk calculations, journaling, "
    "or a Trading Agent command or workflow, call run_trading_agent. Also call it when the user "
    "asks what Trading Agent can do, how to use one of its commands or workflows, what it knows "
    "about their workspace, or about its current configuration or health. The gateway supplies "
    "the complete current tool catalog, runtime policy, selected workspace and account, active "
    "strategy, relevant harness guidance, recent durable conversation history, and PostgreSQL "
    "logging. Do not guess any of that context from general knowledge. Treat its output as "
    "authoritative application state and speak the useful result naturally. Never invent "
    "prices, timestamps, news, fills, indicators, sources, or tool outcomes. Never place, "
    "modify, cancel, hedge, or close an order. Keep normal spoken answers short unless the "
    "user asks for detail. Your name is Pippy, never Jarvis."
)

TRADING_AGENT_TOOL = {
    "type": "function",
    "name": "run_trading_agent",
    "description": (
        "Run a request through Trading Agent's policy-controlled tools, deterministic "
        "calculations, audit trail, and PostgreSQL conversation history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The user's complete request for Trading Agent.",
            }
        },
        "required": ["message"],
        "additionalProperties": False,
    },
}


def create_realtime_client_secret(
    *,
    api_key: str,
    model: str,
    voice: str,
    safety_identifier: str,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Create a short-lived browser credential without returning the project API key."""

    if not valid_model_id(model) or "realtime" not in model.casefold():
        raise ProviderConfigurationError("configured OpenAI Realtime model is invalid")
    if voice not in SUPPORTED_VOICES:
        raise ValueError("unsupported Realtime voice")
    request = urllib.request.Request(
        REALTIME_SESSION_URL,
        data=json.dumps(
            {
                "session": {
                    "type": "realtime",
                    "model": model,
                    "instructions": PIPPY_REALTIME_INSTRUCTIONS,
                    "tools": [TRADING_AGENT_TOOL],
                    "tool_choice": "auto",
                    "audio": {
                        "input": {
                            "transcription": {"model": "gpt-4o-mini-transcribe"},
                            "turn_detection": {
                                "type": "semantic_vad",
                                "eagerness": "low",
                                "create_response": True,
                                "interrupt_response": True,
                            },
                        },
                        "output": {"voice": voice},
                    },
                    "truncation": {
                        "type": "retention_ratio",
                        "retention_ratio": 0.8,
                        "token_limits": {"post_instructions": 8000},
                    },
                }
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "OpenAI-Safety-Identifier": safety_identifier,
        },
        method="POST",
    )
    try:
        # The request URL is a module constant pinned to OpenAI over HTTPS.
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=timeout_seconds,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderConfigurationError(
            f"OpenAI rejected the Realtime session request with status {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProviderConfigurationError(
            "OpenAI Realtime could not create a session"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), str):
        raise ProviderConfigurationError("OpenAI returned an invalid Realtime credential")
    return {
        "value": payload["value"],
        "expires_at": payload.get("expires_at"),
    }
