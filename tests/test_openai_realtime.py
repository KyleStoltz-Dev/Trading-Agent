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


def test_realtime_adapter_uses_mini_tools_and_patient_server_vad() -> None:
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
    assert payload["session"]["tool_choice"] == "required"
    assert "complete current tool catalog" in payload["session"]["instructions"]
    assert "Do not guess any of that context" in payload["session"]["instructions"]
    assert "never ask the user to paste strategies" in payload["session"]["instructions"]
    assert "You are not Trading Agent itself" in payload["session"]["instructions"]
    assert "one unified agent" in payload["session"]["instructions"]
    assert "Pippy remains the voice and orchestration layer" in (
        payload["session"]["tools"][0]["description"]
    )
    assert payload["session"]["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.72,
        "prefix_padding_ms": 250,
        "silence_duration_ms": 900,
        "create_response": True,
        "interrupt_response": True,
    }
    assert "private-key" not in str(result)
