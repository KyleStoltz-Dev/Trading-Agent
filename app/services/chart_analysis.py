import base64
import json

from openai import OpenAI

from app.config import Settings
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
) -> ChartAnalysis:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for chart analysis")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=settings.openai_model,
        reasoning={"effort": "medium"},
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_context},
                    {
                        "type": "input_image",
                        "image_url": f"data:{content_type};base64,{encoded}",
                        "detail": "original",
                    },
                ],
            }
        ],
        instructions=SYSTEM_PROMPT,
        safety_identifier=settings.openai_safety_identifier,
        text={
            "format": {
                "type": "json_schema",
                "name": "chart_analysis",
                "strict": True,
                "schema": ChartAnalysis.model_json_schema(),
            }
        },
    )
    return ChartAnalysis.model_validate(json.loads(response.output_text))
