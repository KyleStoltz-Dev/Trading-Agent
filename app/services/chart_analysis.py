from app.config import Settings
from app.providers import ModelProvider, create_model_provider
from app.schemas import ChartAnalysis

SYSTEM_PROMPT = """
You are a trading-journal evidence assistant, not a signal service or autonomous trader.
Analyze only what is visible in the supplied chart and the user's stated context.
Separate observations from hypotheses. Wyckoff, liquidity, imbalance, manipulation,
mitigation, order-block, and smart-money labels are hypotheses unless independently
defined and measured. Do not infer unreadable prices, timestamps, indicators, fills,
news, or unseen higher/lower timeframes. Never promise an outcome or recommend increasing
risk. Evaluate context, trigger, invalidation, and management separately. Return JSON
matching the supplied schema. The disclaimer must state that this is decision support,
not individualized financial advice.
""".strip()


def analyze_chart(
    image_bytes: bytes,
    content_type: str,
    user_context: str,
    settings: Settings,
    provider: ModelProvider | None = None,
    model: str | None = None,
    reasoning_effort: str = "medium",
) -> ChartAnalysis:
    model_provider = provider or create_model_provider(settings)
    result = model_provider.analyze_chart(
        image_bytes=image_bytes,
        content_type=content_type,
        user_context=user_context,
        instructions=SYSTEM_PROMPT,
        output_schema=ChartAnalysis.model_json_schema(),
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return ChartAnalysis.model_validate(result)
