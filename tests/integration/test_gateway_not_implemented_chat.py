import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from common.schemas import AgentConfig
from services.agent_gateway_v3.app.main import app
from services.agent_gateway_v3.app.api import routes_chat
from services.agent_gateway_v3.app.api import routes_stream
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.backend_types import UpstreamStreamEvent


client = TestClient(app)


def _sse_event_payloads(response_text: str, event_name: str) -> list[dict[str, object]]:
    """Parse JSON event payloads without depending on serializer whitespace."""
    payloads = []
    current_event = None
    for line in response_text.splitlines():
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:") and current_event == event_name:
            value = json.loads(line[len("data:") :].strip())
            if isinstance(value, dict):
                payloads.append(value)
    return payloads


class FakeOpenAIManagedClient:
    def __init__(
        self,
        upstream_events=None,
        delay_seconds=0.0,
        stream_error=None,
        fallback_reply_text=None,
        fallback_error=None,
    ):
        self._upstream_events = upstream_events or [
            UpstreamStreamEvent(
                event_name="agent.session.output_text.delta",
                payload={
                    "type": "agent.session.output_text.delta",
                    "delta": "echo:hello",
                },
            )
        ]
        self._delay_seconds = delay_seconds
        self._stream_error = stream_error
        self._fallback_reply_text = fallback_reply_text
        self._fallback_error = fallback_error
        self.chat_calls: list[dict[str, object]] = []
        self.buffered_query_calls: list[dict[str, object]] = []
        self.stream_calls: list[dict[str, object]] = []

    async def ensure_session(self, *, agent_config, user_id, request):
        return SimpleNamespace(
            session_id=request.session_id or "session-fake",
            thread_id=request.thread_id or "thread-fake",
        )

    async def chat(self, *, agent_config, user_id, session_id, message):
        self.chat_calls.append(
            {"agent_id": agent_config.agent_id, "user_id": user_id, "session_id": session_id, "message": message}
        )
        raise AssertionError("buffered route must use chat_buffered_query")

    async def chat_buffered_query(
        self, *, agent_config, user_id, session_id, message, idempotency_key=None
    ):
        self.buffered_query_calls.append(
            {"agent_id": agent_config.agent_id, "user_id": user_id, "session_id": session_id, "message": message}
        )
        if self._fallback_error is not None:
            raise self._fallback_error
        reply_text = self._fallback_reply_text or f"echo:{message}"
        return SimpleNamespace(
            reply_text=reply_text,
            event_count=1,
            usage={"provider": "openai_agents_api", "billing_status": "pending_worker_processing"},
        )

    async def stream_chat_events(
        self, *, agent_config, user_id, session_id, message, idempotency_key=None
    ):
        self.stream_calls.append(
            {"agent_id": agent_config.agent_id, "user_id": user_id, "session_id": session_id, "message": message}
        )
        if self._stream_error is not None:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            raise self._stream_error

        for upstream_event in self._upstream_events:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            yield upstream_event

    def extract_text_fragments(self, event):
        event_type = str(event.get("type", ""))
        if event_type.endswith("output_text.delta"):
            delta = event.get("delta")
            return [delta] if isinstance(delta, str) and delta else []
        if event_type.endswith("output_text.done"):
            text = event.get("text")
            return [text] if isinstance(text, str) and text else []
        return []


class FakePublisher:
    async def publish_turn_completed(self, event):
        return SimpleNamespace(message_id="msg-fake")


class FakeWalletReservationService:
    def __init__(self):
        self.calls: list[dict[str, object]] = []
        self.releases: list[object] = []

    async def reserve(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            event_metadata=lambda: {
                "reservation_id": kwargs["turn_id"],
                "billing_subject_id": kwargs["user_id"],
                "reserved_amount_nanos": 500_000_000,
                "currency": "USD",
            }
        )

    async def release(self, reservation):
        self.releases.append(reservation)


async def _fake_authenticate_request(request) -> str:
    return "user-test"


async def _fake_get_openai_managed_client() -> FakeOpenAIManagedClient:
    return FakeOpenAIManagedClient()


async def _fake_get_pubsub_publisher() -> FakePublisher:
    return FakePublisher()


def _fake_get_wallet_reservation_service(service):
    async def _factory():
        return service

    return _factory


def _fake_get_streaming_agent_config(agent_id: str) -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        backend="openai_managed",
        remote_agent_id="agent_test123",
        model="gpt-6-luna",
        streaming_enabled=True,
    )


def _enable_streaming_route(monkeypatch) -> None:
    monkeypatch.setattr(routes_stream, "get_agent_config", _fake_get_streaming_agent_config)


def _make_runtime_client(
    upstream_events=None,
    delay_seconds=0.0,
    stream_error=None,
    fallback_reply_text=None,
    fallback_error=None,
):
    async def _fake_runtime_client():
        return FakeOpenAIManagedClient(
            upstream_events=upstream_events,
            delay_seconds=delay_seconds,
            stream_error=stream_error,
            fallback_reply_text=fallback_reply_text,
            fallback_error=fallback_error,
        )

    return _fake_runtime_client


def test_buffered_chat_returns_structured_success(monkeypatch) -> None:
    runtime_client = FakeOpenAIManagedClient()

    async def _runtime_client():
        return runtime_client

    monkeypatch.setattr(routes_chat, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(routes_chat, "get_chat_backend_client", lambda _agent_config: _runtime_client())
    monkeypatch.setattr(routes_chat, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/openai_managed_agent/chat", json={"message": "hello"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["agent_id"] == "openai_managed_agent"
    assert payload["thread_id"] == "thread-fake"
    assert payload["session_id"] == "session-fake"
    assert payload["reply_text"] == "echo:hello"
    assert payload["usage"]["provider"] == "openai_agents_api"
    assert len(runtime_client.chat_calls) == 0
    assert len(runtime_client.stream_calls) == 0
    assert len(runtime_client.buffered_query_calls) == 1
    assert runtime_client.buffered_query_calls[0]["message"] == "hello"


def test_buffered_chat_rejects_unknown_agent(monkeypatch) -> None:
    monkeypatch.setattr(routes_chat, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(
        routes_chat,
        "get_chat_backend_client",
        lambda _agent_config: _fake_get_openai_managed_client(),
    )
    monkeypatch.setattr(routes_chat, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/unknown/chat", json={"message": "hello"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_agent"





def test_buffered_chat_propagates_the_server_created_billing_reservation(monkeypatch) -> None:
    runtime_client = FakeOpenAIManagedClient()
    published_events = []
    reservation_service = FakeWalletReservationService()

    async def _backend_client(_agent_config):
        return runtime_client

    class CapturingPublisher:
        async def publish_turn_completed(self, event):
            published_events.append(event)
            return SimpleNamespace(message_id="msg-billing")

    async def _publisher():
        return CapturingPublisher()

    monkeypatch.setattr(routes_chat, "authenticate_request", _fake_authenticate_request)
    monkeypatch.setattr(routes_chat, "get_chat_backend_client", _backend_client)
    monkeypatch.setattr(routes_chat, "get_pubsub_publisher", _publisher)
    monkeypatch.setattr(
        routes_chat,
        "get_wallet_reservation_service",
        _fake_get_wallet_reservation_service(reservation_service),
    )

    response = client.post("/v1/agents/openai_managed_agent/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert len(reservation_service.calls) == 1
    assert len(published_events) == 1
    billing = published_events[0].metadata["billing"]
    assert billing["reservation_id"] == published_events[0].turn_id
    assert billing["billing_subject_id"] == "user-test"


def test_stream_chat_rejects_when_streaming_disabled(monkeypatch) -> None:
    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    disabled_config = AgentConfig(
        agent_id="openai_managed_agent",
        backend="openai_managed",
        remote_agent_id="agent_test123",
        model="gpt-6-luna",
        streaming_enabled=False,
    )
    monkeypatch.setattr(routes_stream, "get_agent_config", lambda _id: disabled_config)

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "streaming_not_enabled"
    assert "/v1/agents/openai_managed_agent/chat" in payload["error"]["message"]


def test_stream_chat_returns_normalized_sse_contract(monkeypatch) -> None:
    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _fake_get_openai_managed_client(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "event: metadata" in response.text
    assert {"text": "echo:hello"} in _sse_event_payloads(response.text, "token")
    assert "event: done" in response.text
    done_payloads = _sse_event_payloads(response.text, "done")
    assert any(payload.get("reply_text") == "echo:hello" for payload in done_payloads)
    assert any(payload.get("pubsub_message_id") == "msg-fake" for payload in done_payloads)





def test_stream_chat_emits_status_while_waiting_for_upstream(monkeypatch) -> None:
    upstream_events = [
        UpstreamStreamEvent(
                event_name="agent.session.output_text.delta",
            payload={
                    "type": "agent.session.output_text.delta",
                    "delta": "echo:hello",
            },
        )
    ]

    monkeypatch.setattr(routes_stream, "STREAM_STATUS_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            upstream_events=upstream_events,
            delay_seconds=0.08,
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: status") >= 2
    assert response.text.index("event: metadata") < response.text.index("event: status")
    assert response.text.index("event: status") < response.text.index("event: token")
    assert any(
        payload.get("phase") == "waiting_for_runtime"
        for payload in _sse_event_payloads(response.text, "status")
    )
    assert any(
        payload.get("reply_text") == "echo:hello"
        for payload in _sse_event_payloads(response.text, "done")
    )


def test_stream_chat_emits_status_error_and_done_on_upstream_read_timeout(monkeypatch) -> None:
    log_calls: list[dict[str, object]] = []
    stream_error = ApiError(
        504,
        "openai_managed_read_timeout",
        "The OpenAI managed agent exceeded the stream idle timeout.",
        {
            "timeout_type": "read",
            "timeout_seconds": 240,
            "reason": "read timed out",
        },
    )

    def _capture_log(_logger, _level, event, **fields):
        log_calls.append({"event": event, **fields})

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            stream_error=stream_error,
            fallback_error=RuntimeError("fallback unavailable"),
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(routes_stream, "log_structured", _capture_log)
    monkeypatch.setattr(
        routes_stream,
        "get_settings",
        lambda: SimpleNamespace(
            stream_debug=False,
            upstream_connect_timeout_seconds=10,
            upstream_read_timeout_seconds=240,
            upstream_total_timeout_seconds=570,
        ),
    )

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert "event: metadata" in response.text
    assert "event: status" in response.text
    assert "event: error" in response.text
    assert "event: done" in response.text
    assert any(
        payload.get("code") == "openai_managed_read_timeout"
        for payload in _sse_event_payloads(response.text, "error")
    )
    assert any(payload.get("ok") is False for payload in _sse_event_payloads(response.text, "done"))

    failure_log = next(
        call for call in log_calls if call["event"] == "gateway_stream_failed"
    )
    assert failure_log["code"] == "openai_managed_read_timeout"
    assert failure_log["timeout_type"] == "read"
    assert failure_log["upstream_read_timeout_seconds"] == 240
    assert failure_log["upstream_elapsed_ms"] is not None


def test_stream_chat_falls_back_to_buffered_before_any_token(monkeypatch) -> None:
    stream_error = ApiError(
        504,
        "openai_managed_read_timeout",
        "The OpenAI managed agent exceeded the stream idle timeout.",
        {"timeout_type": "read", "timeout_seconds": 240},
    )

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            stream_error=stream_error,
            fallback_reply_text="fallback response",
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert "event: error" not in response.text
    assert {"text": "fallback response"} in _sse_event_payloads(response.text, "token")
    assert any(
        payload.get("fallback") is True
        and payload.get("fallback_from_code") == "openai_managed_read_timeout"
        for payload in _sse_event_payloads(response.text, "done")
    )


def test_stream_chat_does_not_fallback_after_token(monkeypatch) -> None:
    stream_error = ApiError(
        502,
        "openai_managed_unreachable",
        "The OpenAI Agents API stream could not be reached.",
    )

    class TokenThenErrorClient(FakeOpenAIManagedClient):
        async def stream_chat_events(
            self, *, agent_config, user_id, session_id, message, idempotency_key=None
        ):
            yield UpstreamStreamEvent(
                event_name="agent.session.output_text.delta",
                payload={
                    "type": "agent.session.output_text.delta",
                    "delta": "partial",
                },
            )
            raise stream_error

        async def chat_buffered_query(
            self, *, agent_config, user_id, session_id, message, idempotency_key=None
        ):
            raise AssertionError("fallback must not run after a token was emitted")

    async def _openai_client():
        return TokenThenErrorClient()

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _openai_client(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert {"text": "partial"} in _sse_event_payloads(response.text, "token")
    assert "event: error" in response.text
    assert any(payload.get("ok") is False for payload in _sse_event_payloads(response.text, "done"))


def test_stream_chat_logs_fragment_counters_for_multiple_text_events(monkeypatch) -> None:
    log_calls: list[dict[str, object]] = []
    upstream_events = [
        UpstreamStreamEvent(
            event_name="agent.session.output_text.delta",
            payload={
                "type": "agent.session.output_text.delta",
                "delta": "echo:",
            },
        ),
        UpstreamStreamEvent(
            event_name="agent.session.output_text.delta",
            payload={
                "type": "agent.session.output_text.delta",
                "delta": "hello",
            },
        ),
    ]

    def _capture_log(_logger, _level, event, **fields):
        log_calls.append({"event": event, **fields})

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(upstream_events=upstream_events)(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(routes_stream, "log_structured", _capture_log)

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: token") == 2
    assert any(
        payload.get("reply_text") == "echo:hello"
        for payload in _sse_event_payloads(response.text, "done")
    )

    completion_log = next(
        call for call in log_calls if call["event"] == "gateway_stream_completed"
    )
    assert completion_log["upstream_sse_message_count"] == 2
    assert completion_log["upstream_text_event_count"] == 2
    assert completion_log["upstream_text_fragment_count"] == 2
    assert completion_log["normalized_token_event_count"] == 2
    assert completion_log["reply_text_char_count"] == len("echo:hello")
    assert completion_log["first_token_latency_ms"] is not None


def test_stream_chat_emits_only_new_suffix_for_cumulative_partials(monkeypatch) -> None:
    upstream_events = [
        UpstreamStreamEvent(
            event_name="agent.session.output_text.delta",
            payload={
                "type": "agent.session.output_text.delta",
                "delta": "echo:",
            },
        ),
        UpstreamStreamEvent(
            event_name="agent.session.output_text.delta",
            payload={
                "type": "agent.session.output_text.delta",
                "delta": "echo:hello",
            },
        ),
    ]

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(upstream_events=upstream_events)(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: token") == 2
    token_payloads = _sse_event_payloads(response.text, "token")
    assert {"text": "echo:"} in token_payloads
    assert {"text": "hello"} in token_payloads
    assert any(
        payload.get("reply_text") == "echo:hello"
        for payload in _sse_event_payloads(response.text, "done")
    )


def test_stream_chat_debug_log_captures_upstream_shape_without_text(monkeypatch) -> None:
    log_calls: list[dict[str, object]] = []
    upstream_events = [
        UpstreamStreamEvent(
            event_name="metadata",
            payload={"type": "metadata", "sequence": 1},
        ),
        UpstreamStreamEvent(
            event_name="agent.session.output_text.delta",
            payload={
                "type": "agent.session.output_text.delta",
                "delta": "echo:hello",
            },
        ),
    ]

    def _capture_log(_logger, _level, event, **fields):
        log_calls.append({"event": event, **fields})

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(upstream_events=upstream_events)(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(routes_stream, "log_structured", _capture_log)
    monkeypatch.setattr(
        routes_stream,
        "get_settings",
        lambda: SimpleNamespace(stream_debug=True, upstream_total_timeout_seconds=570),
    )

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.count("event: token") == 1

    debug_log = next(
        call for call in log_calls
        if call["event"] == "gateway_stream_upstream_diagnostics"
    )
    assert debug_log["outcome"] == "completed"
    assert debug_log["upstream_sse_message_count"] == 2
    assert debug_log["upstream_text_event_count"] == 1
    assert debug_log["upstream_text_fragment_count"] == 1
    assert debug_log["normalized_token_event_count"] == 1
    assert debug_log["upstream_event_names"] == [
        "metadata",
        "agent.session.output_text.delta",
    ]
    assert debug_log["upstream_fragment_counts"] == [0, 1]
    assert debug_log["upstream_payload_keys"] == [
        ["sequence", "type"],
        ["delta", "type"],
    ]
    assert debug_log["upstream_fragment_lengths"] == [[], [len("echo:hello")]]
    assert "echo:hello" not in str(debug_log)


def test_stream_chat_releases_reservation_on_confirmed_connect_timeout(monkeypatch) -> None:
    """A confirmed pre-request connect timeout must release the billing reservation."""
    connect_error = ApiError(
        504,
        "openai_managed_connect_timeout",
        "The gateway timed out while opening a streaming connection.",
        {"timeout_type": "connect", "reason": "connect timed out"},
    )
    reservation_service = FakeWalletReservationService()

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            stream_error=connect_error,
            fallback_error=RuntimeError("no fallback"),
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(
        routes_stream,
        "get_wallet_reservation_service",
        _fake_get_wallet_reservation_service(reservation_service),
    )
    monkeypatch.setattr(
        routes_stream,
        "get_settings",
        lambda: SimpleNamespace(
            stream_debug=False,
            upstream_connect_timeout_seconds=10,
            upstream_read_timeout_seconds=240,
            upstream_total_timeout_seconds=570,
        ),
    )

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert any(
        payload.get("code") == "openai_managed_connect_timeout"
        for payload in _sse_event_payloads(response.text, "error")
    )
    # Connect timeout is confirmed pre-request failure: hold MUST be released
    assert len(reservation_service.releases) == 1


def test_stream_chat_retains_reservation_on_post_connection_read_timeout(monkeypatch) -> None:
    """A post-connection read timeout must NOT release the reservation (retain for reconciliation)."""
    read_error = ApiError(
        504,
        "openai_managed_read_timeout",
        "The gateway timed out while reading stream data.",
        {"timeout_type": "read", "reason": "read timed out"},
    )
    reservation_service = FakeWalletReservationService()

    monkeypatch.setattr(routes_stream, "authenticate_request", _fake_authenticate_request)
    _enable_streaming_route(monkeypatch)
    monkeypatch.setattr(
        routes_stream,
        "get_streaming_chat_backend_client",
        lambda _agent_config: _make_runtime_client(
            stream_error=read_error,
            fallback_error=RuntimeError("no fallback"),
        )(),
    )
    monkeypatch.setattr(routes_stream, "get_pubsub_publisher", _fake_get_pubsub_publisher)
    monkeypatch.setattr(
        routes_stream,
        "get_wallet_reservation_service",
        _fake_get_wallet_reservation_service(reservation_service),
    )
    monkeypatch.setattr(
        routes_stream,
        "get_settings",
        lambda: SimpleNamespace(
            stream_debug=False,
            upstream_connect_timeout_seconds=10,
            upstream_read_timeout_seconds=240,
            upstream_total_timeout_seconds=570,
        ),
    )

    response = client.post("/v1/agents/openai_managed_agent/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert any(
        payload.get("code") == "openai_managed_read_timeout"
        for payload in _sse_event_payloads(response.text, "error")
    )
    # Read timeout is post-request-acceptance: hold must NOT be released, retained for reconciliation!
    assert len(reservation_service.releases) == 0

