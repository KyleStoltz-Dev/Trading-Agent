import json
from unittest.mock import patch

from app.providers.openai_realtime import create_realtime_client_secret


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({"value": "ephemeral", "expires_at": 1234}).encode()


def test_realtime_adapter_uses_mini_tools_and_low_eagerness_vad() -> None:
    with patch(
        "app.providers.openai_realtime.urllib.request.urlopen",
        return_value=FakeResponse(),
    ) as send:
        result = create_realtime_client_secret(
            api_key="private-key",
            model="gpt-realtime-2.1-mini",
            voice="marin",
            safety_identifier="local-user",
        )

    assert result == {"value": "ephemeral", "expires_at": 1234}
    request = send.call_args.args[0]
    payload = json.loads(request.data)
    assert request.headers["Authorization"] == "Bearer private-key"
    assert payload["session"]["model"] == "gpt-realtime-2.1-mini"
    assert payload["session"]["tools"][0]["name"] == "run_trading_agent"
    assert "complete current tool catalog" in payload["session"]["instructions"]
    assert "Do not guess any of that context" in payload["session"]["instructions"]
    assert payload["session"]["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": True,
        "interrupt_response": True,
    }
    assert "private-key" not in str(result)
