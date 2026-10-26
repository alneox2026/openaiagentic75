import json

from services.agent_gateway_v3.app.services.sse_adapter import format_sse


def test_format_sse_emits_expected_frame() -> None:
    payload = format_sse("token", {"text": "hello"})
    assert payload.startswith("event: token\n")
    data_line = next(line for line in payload.splitlines() if line.startswith("data:"))
    assert json.loads(data_line[len("data:") :].strip()) == {"text": "hello"}
    assert payload.endswith("\n\n")

