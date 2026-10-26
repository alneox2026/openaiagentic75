from types import SimpleNamespace

from fastapi.testclient import TestClient

from services.agent_gateway_v3.app.api import routes_chat, routes_stream
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.main import app
from services.agent_gateway_v3.app.services.agent_registry import load_registry
from services.agent_gateway_v3.app.services.chat_session_service import ChatSessionService


class TrackingBackendClient:
    def __init__(self):
        self.session_create_calls = 0
        self.agent_calls = 0

    async def ensure_session(self, *, agent_config, user_id, request):
        self.session_create_calls += 1
        return SimpleNamespace(session_id="session-fake", thread_id="thread-fake")

    async def chat_buffered_query(self, **kwargs):
        self.agent_calls += 1
        raise AssertionError("the provider must not be called when wallet reservation fails")

    async def stream_chat_events(self, **kwargs):
        self.agent_calls += 1
        raise AssertionError("the provider must not be called when wallet reservation fails")
        yield  # Make this an async generator for the streaming backend protocol.

    def extract_text_fragments(self, event):
        return []


class InsufficientCreditReservationService:
    async def reserve(self, **kwargs):
        raise ApiError(402, "insufficient_credit", "Insufficient available wallet credit.")

    async def release(self, reservation):
        raise AssertionError("a failed reservation must not be released")


def _config_with(config, **updates):
    values = config.model_dump() if hasattr(config, "model_dump") else config.dict()
    values.update(updates)
    return type(config)(**values)


def _find_configured_agent_id():
    agent_id = next(iter(load_registry()))
    return agent_id, routes_chat.get_agent_config(agent_id)


def _install_zero_credit_preflight(monkeypatch, backend_client):
    async def authenticate(_request):
        return "user-zero-credit"

    async def get_session_service():
        return ChatSessionService()

    async def get_reservation_service():
        return InsufficientCreditReservationService()

    async def get_buffered_backend(_agent_config):
        return backend_client

    async def get_streaming_backend(_agent_config):
        return backend_client

    agent_id, agent_config = _find_configured_agent_id()
    monkeypatch.setattr(routes_chat, "authenticate_request", authenticate)
    monkeypatch.setattr(routes_chat, "get_agent_config", lambda _agent_id: _config_with(
        agent_config, persistence_enabled=True
    ))
    monkeypatch.setattr(routes_chat, "get_chat_backend_client", get_buffered_backend)
    monkeypatch.setattr(routes_chat, "get_chat_session_service", get_session_service)
    monkeypatch.setattr(routes_chat, "get_wallet_reservation_service", get_reservation_service)

    streaming_config = _config_with(
        agent_config,
        persistence_enabled=True,
        streaming_enabled=True,
    )
    monkeypatch.setattr(routes_stream, "authenticate_request", authenticate)
    monkeypatch.setattr(routes_stream, "get_agent_config", lambda _agent_id: streaming_config)
    monkeypatch.setattr(routes_stream, "get_streaming_chat_backend_client", get_streaming_backend)
    monkeypatch.setattr(routes_stream, "get_chat_session_service", get_session_service)
    monkeypatch.setattr(routes_stream, "get_wallet_reservation_service", get_reservation_service)
    return agent_id


def test_zero_credit_blocks_buffered_request_before_provider_session_creation(monkeypatch):
    backend_client = TrackingBackendClient()
    agent_id = _install_zero_credit_preflight(monkeypatch, backend_client)

    response = TestClient(app).post(
        f"/v1/agents/{agent_id}/chat",
        json={"message": "this must not reach the provider"},
    )

    assert response.status_code == 402
    assert backend_client.session_create_calls == 0
    assert backend_client.agent_calls == 0


def test_zero_credit_blocks_streaming_request_before_provider_session_creation(monkeypatch):
    backend_client = TrackingBackendClient()
    agent_id = _install_zero_credit_preflight(monkeypatch, backend_client)

    response = TestClient(app).post(
        f"/v1/agents/{agent_id}/chat/stream",
        json={"message": "this must not reach the provider"},
    )

    assert response.status_code == 402
    assert backend_client.session_create_calls == 0
    assert backend_client.agent_calls == 0
