import asyncio
from types import SimpleNamespace

import pytest

from services.agent_gateway_v3.app.api.routes_stream import (
    _stream_upstream_events_with_heartbeats,
)
from services.agent_gateway_v3.app.core.errors import ApiError


class _SlowBackend:
    def __init__(self) -> None:
        self.closed = False

    async def stream_chat_events(self, **kwargs):
        try:
            await asyncio.sleep(0.5)
            yield SimpleNamespace(event_name="message", payload={})
        finally:
            self.closed = True


def test_upstream_stream_has_an_absolute_wall_clock_deadline() -> None:
    backend = _SlowBackend()

    async def _collect() -> None:
        async for _ in _stream_upstream_events_with_heartbeats(
            backend_client=backend,
            agent_config=SimpleNamespace(agent_id="test-agent"),
            user_id="user-1",
            session_id="session-1",
            message="hello",
            timeout_seconds=0.05,
        ):
            pass

    with pytest.raises(ApiError) as error:
        asyncio.run(_collect())

    assert error.value.code == "upstream_turn_timeout"
    assert error.value.details["timeout_type"] == "total"
    assert backend.closed


def test_cancelling_stream_closes_upstream_and_heartbeat_tasks() -> None:
    backend = _SlowBackend()

    async def _cancel_consumer() -> None:
        async def _collect() -> None:
            async for _ in _stream_upstream_events_with_heartbeats(
                backend_client=backend,
                agent_config=SimpleNamespace(agent_id="test-agent"),
                user_id="user-1",
                session_id="session-1",
                message="hello",
                timeout_seconds=1,
            ):
                pass

        task = asyncio.create_task(_collect())
        await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel_consumer())
    assert backend.closed
