"""Conservative cloud-model catalog reviewed against the provider adapters."""

from app.providers.base import valid_model_id

SUPPORTED_CLOUD_AGENT_MODELS: dict[str, frozenset[str]] = {
    "openai": frozenset(
        {
            "gpt-5.6-sol",
            "gpt-5.6",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        }
    ),
    "anthropic": frozenset({"claude-sonnet-5"}),
}


def supported_agent_model(provider: str, model: str) -> bool:
    """Return whether the adapter request shape is reviewed for this model."""
    if provider == "ollama":
        return valid_model_id(model)
    return model in SUPPORTED_CLOUD_AGENT_MODELS.get(provider, frozenset())
