import json

import httpx
import pytest

from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.openai_managed_client import (
    OpenAIManagedClient,
    _error,
)
from services.agent_gateway_v3.app.services.turn_assembler import TurnAssembler


def _agent_config(model: str = "gpt-6-luna") -> AgentConfig:
    return AgentConfig(
        agent_id="openai_managed_agent",
        backend="openai_managed",
        remote_agent_id="agent_test123",
        model=model,
    )


def test_upstream_error_redacts_openai_api_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")
    error = _error(401, '{"error":{"message":"credential secret-test-key rejected"}}')

    assert "secret-test-key" not in str(error.details)
    assert "[REDACTED]" in str(error.details)


@pytest.mark.anyio
async def test_saved_agent_session_stream_and_usage_flow(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    request_order: list[tuple[str, str]] = []
    stream_body = "\n".join(
        [
            "event: agent.session.turn.output_text.delta",
            'data: {"type":"agent.session.turn.output_text.delta","delta":"Hello"}',
            "",
            "event: agent.session.turn.completed",
            'data: {"type":"agent.session.turn.completed","turn":{"id":"turn_1","subagent_id":null,"usage":{"input_tokens":1000,"input_tokens_details":{"cached_tokens":200},"output_tokens":100,"output_tokens_details":{"reasoning_tokens":20}}}}',
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        request_order.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/agents/sessions"):
            body = json.loads(request.content)
            assert body == {
                "agent_id": "agent_test123",
                "environment": {"type": "openai_hosted"},
            }
            return httpx.Response(
                200,
                json={"id": "sess_1", "agent": {"id": "agent_test123", "model": "gpt-6-luna"}},
            )
        if request.method == "GET" and request.url.path.endswith("/events"):
            assert request.headers["OpenAI-Beta"] == "agents=v1"
            assert request.headers["Accept"] == "text/event-stream"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=stream_body.encode(),
            )
        if request.method == "POST" and request.url.path.endswith("/events"):
            assert request.headers["Idempotency-Key"] == "stable-turn-id"
            body = json.loads(request.content)
            assert body["events"][0]["type"] == "agent.session.input.message"
            assert body["events"][0]["input"][0]["content"][0]["text"] == "Hello there"
            return httpx.Response(202)
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIManagedClient(http_client=http_client)
    session = await client.ensure_session(
        agent_config=_agent_config(),
        user_id="firebase-user",
        request=ChatRequest(message="Hello there"),
    )
    assert session.session_id == "sess_1"

    assembler = TurnAssembler()
    async for event in client.stream_chat_events(
        agent_config=_agent_config(),
        user_id="firebase-user",
        session_id=session.session_id,
        message="Hello there",
        idempotency_key="stable-turn-id",
    ):
        assembler.add_event(event.payload)
        for text in client.extract_text_fragments(event.payload):
            assembler.add_text(text)

    assert request_order[0] == ("POST", "/v1/agents/sessions")
    assert request_order[1] == ("GET", "/v1/agents/sessions/sess_1/events")
    assert request_order[2] == ("POST", "/v1/agents/sessions/sess_1/events")
    assert len(request_order) == 3
    assert assembler.reply_text() == "Hello"
    assert assembler.usage["billing_status"] == "pending_worker_processing"
    assert assembler.usage["raw_turn_usage_reports"] == [
        {
            "turn_id": "turn_1",
            "subagent_id": None,
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens": 100,
                "output_tokens_details": {"reasoning_tokens": 20},
            },
        }
    ]
    assert "estimated_cost_usd" not in assembler.usage
    await client.close()


@pytest.mark.anyio
async def test_fails_closed_if_saved_agent_model_differs_from_pricing_model(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "sess_1", "agent": {"id": "agent_test123", "model": "gpt-other"}},
        )

    client = OpenAIManagedClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ApiError) as exc_info:
        await client.ensure_session(
            agent_config=_agent_config(),
            user_id="firebase-user",
            request=ChatRequest(message="hello"),
        )
    assert exc_info.value.code == "openai_agent_model_mismatch"
    await client.close()


@pytest.mark.anyio
async def test_fails_closed_if_saved_agent_model_is_not_returned(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    client = OpenAIManagedClient(
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"id": "sess_1"})
            )
        )
    )

    with pytest.raises(ApiError) as exc_info:
        await client.ensure_session(
            agent_config=_agent_config(),
            user_id="firebase-user",
            request=ChatRequest(message="hello"),
        )

    assert exc_info.value.code == "openai_agent_model_unavailable"
    await client.close()


@pytest.mark.anyio
async def test_requires_api_key_before_upstream_request(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("no request")))
    client = OpenAIManagedClient(http_client=http_client)
    with pytest.raises(ApiError) as exc_info:
        await client.ensure_session(
            agent_config=_agent_config(),
            user_id="firebase-user",
            request=ChatRequest(message="hello"),
        )
    assert exc_info.value.code == "openai_api_key_missing"
    await client.close()
