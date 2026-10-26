"""Raw HTTP client for OpenAI's managed Agents API (not the Agents SDK)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx

from common.ids import new_thread_id
from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.backend_types import (
    BufferedAgentResponse,
    SessionResult,
    UpstreamStreamEvent,
)
from services.agent_gateway_v3.app.services.turn_assembler import TurnAssembler

OPENAI_AGENTS_API_BASE_URL = "https://api.openai.com/v1"
OPENAI_AGENTS_BETA_HEADER = "agents=v1"
ROOT_TERMINAL_EVENTS = {
    "agent.session.turn.completed",
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
}
_client_singleton: "OpenAIManagedClient | None" = None
_client_lock = asyncio.Lock()


def _error(status_code: int, body: str) -> ApiError:
    try:
        parsed = json.loads(body)
        detail = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
        detail = str(detail or body)
    except (TypeError, ValueError):
        detail = body
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    detail = detail[:1200]
    if status_code in {401, 403}:
        code = "openai_managed_auth_error"
        message = "OpenAI rejected the middleware API credentials or project permissions."
    elif status_code == 429:
        code = "openai_managed_rate_limited"
        message = "OpenAI rate-limited this managed agent request."
    else:
        code = "openai_managed_upstream_error"
        message = f"OpenAI Agents API returned HTTP {status_code}."
    return ApiError(
        502 if status_code < 500 else status_code,
        code,
        message,
        {"upstream_status_code": status_code, "detail": detail},
    )


def _payload_session_model(payload: dict[str, Any]) -> str | None:
    agent = payload.get("agent")
    if not isinstance(agent, dict):
        return None
    model = agent.get("model")
    return str(model).strip() if model else None


def _turn_from_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    turn = payload.get("turn")
    return turn if isinstance(turn, dict) else None


def _event_name(event_name: str | None, payload: dict[str, Any]) -> str | None:
    value = payload.get("type")
    return str(value) if value else event_name


class OpenAIManagedClient:
    """HTTP implementation of saved-agent sessions, events, and turn usage."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 570.0,
        max_connections: int = 200,
        max_keepalive_connections: int = 50,
        keepalive_expiry_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = OPENAI_AGENTS_API_BASE_URL,
    ) -> None:
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.base_url = base_url.rstrip("/")
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=30.0,
                pool=connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_keepalive_connections=max_keepalive_connections,
                max_connections=max_connections,
                keepalive_expiry=keepalive_expiry_seconds,
            ),
        )

    async def close(self) -> None:
        await self._http_client.aclose()

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ApiError(
                503,
                "openai_api_key_missing",
                "The OpenAI managed-agent API key is not configured on the gateway.",
            )
        return {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": OPENAI_AGENTS_BETA_HEADER,
            "Accept": accept,
        }

    async def ensure_session(
        self,
        *,
        agent_config: AgentConfig,
        user_id: str,
        request: ChatRequest,
    ) -> SessionResult:
        del user_id, request
        if not agent_config.remote_agent_id:
            raise ApiError(
                503,
                "openai_agent_id_missing",
                "This middleware agent is missing its saved OpenAI Agent ID.",
            )
        body = {
            "agent_id": agent_config.remote_agent_id,
            "environment": {"type": "openai_hosted"},
        }
        try:
            response = await self._http_client.post(
                f"{self.base_url}/agents/sessions",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ApiError(
                504,
                "openai_managed_connect_timeout",
                "Could not connect to the OpenAI Agents API before creating a session.",
                {"timeout_type": "connect", "detail": str(exc)},
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                502,
                "openai_managed_unreachable",
                "The OpenAI Agents API could not be reached.",
                {"timeout_type": "connect", "detail": str(exc)},
            ) from exc
        if response.status_code not in {200, 201}:
            raise _error(response.status_code, response.text)
        data = response.json()
        session_id = str(data.get("id", "")).strip()
        if not session_id:
            raise ApiError(
                502,
                "openai_session_id_missing",
                "OpenAI created a session response without a session ID.",
            )
        actual_model = _payload_session_model(data)
        expected_model = (agent_config.model or "").strip()
        if not actual_model:
            raise ApiError(
                503,
                "openai_agent_model_unavailable",
                "OpenAI did not return the saved Agent model needed to select a safe billing rate card.",
                {"pricing_action": "Confirm the saved Agent model before serving requests."},
            )
        if actual_model != expected_model:
            raise ApiError(
                503,
                "openai_agent_model_mismatch",
                "The saved OpenAI Agent model does not match the middleware billing catalog model.",
                {
                    "configured_model": expected_model,
                    "saved_agent_model": actual_model,
                    "pricing_action": "Update the registry model and its versioned rate card before serving requests.",
                },
            )
        return SessionResult(session_id=session_id, thread_id=new_thread_id())

    async def chat_buffered_query(
        self,
        *,
        agent_config: AgentConfig,
        user_id: str,
        session_id: str,
        message: str,
        idempotency_key: str | None = None,
    ) -> BufferedAgentResponse:
        assembler = TurnAssembler()
        event_count = 0
        async for event in self.stream_chat_events(
            agent_config=agent_config,
            user_id=user_id,
            session_id=session_id,
            message=message,
            idempotency_key=idempotency_key,
        ):
            event_count += 1
            assembler.add_event(event.payload)
            for fragment in self.extract_text_fragments(event.payload):
                assembler.add_text(fragment)
        return BufferedAgentResponse(
            reply_text=assembler.reply_text(),
            event_count=event_count,
            usage=assembler.usage,
        )

    async def stream_chat_events(
        self,
        *,
        agent_config: AgentConfig,
        user_id: str,
        session_id: str,
        message: str,
        idempotency_key: str | None = None,
    ) -> AsyncIterator[UpstreamStreamEvent]:
        del user_id
        headers = self._headers(accept="text/event-stream")
        events_url = f"{self.base_url}/agents/sessions/{session_id}/events?stream=true"
        input_url = f"{self.base_url}/agents/sessions/{session_id}/events"
        submission_key = (idempotency_key or uuid4().hex).strip()
        turn_usage: dict[str, dict[str, Any]] = {}
        completed_turn_count = 0
        root_terminal_seen = False
        root_terminal_event: str | None = None

        try:
            async with self._http_client.stream(
                "GET",
                events_url,
                headers=headers,
            ) as stream_response:
                if stream_response.status_code != 200:
                    body = (await stream_response.aread()).decode("utf-8", errors="replace")
                    raise _error(stream_response.status_code, body)

                # Subscribe first, then submit. Idempotency-Key makes an uncertain
                # POST retry safe for the same logical middleware turn.
                try:
                    submitted = await self._http_client.post(
                        input_url,
                        headers={
                            **headers,
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "Idempotency-Key": submission_key,
                        },
                        json={
                            "events": [
                                {
                                    "type": "agent.session.input.message",
                                    "input": [
                                        {
                                            "role": "user",
                                            "content": [
                                                {"type": "input_text", "text": message}
                                            ],
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    raise ApiError(
                        504,
                        "openai_managed_connect_timeout",
                        "Could not submit the message to OpenAI before the request connected.",
                        {"timeout_type": "connect", "detail": str(exc)},
                    ) from exc
                if submitted.status_code not in {200, 201, 202, 204}:
                    raise _error(submitted.status_code, submitted.text)

                event_name: str | None = None
                data_lines: list[str] = []
                async for line in stream_response.aiter_lines():
                    if not line:
                        if data_lines:
                            parsed = self._parse_event(event_name, data_lines)
                            data_lines = []
                            event_name = None
                            if parsed is not None:
                                payload, name = parsed
                                terminal = self._record_turn_usage(
                                    payload,
                                    name,
                                    turn_usage,
                                )
                                if terminal:
                                    completed_turn_count += 1
                                yield UpstreamStreamEvent(event_name=name, payload=payload)
                                if terminal and name in ROOT_TERMINAL_EVENTS:
                                    turn = _turn_from_event(payload) or {}
                                    if turn.get("subagent_id") in (None, ""):
                                        root_terminal_seen = True
                                        root_terminal_event = name
                                        break
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())

                if not root_terminal_seen and data_lines:
                    parsed = self._parse_event(event_name, data_lines)
                    if parsed is not None:
                        payload, name = parsed
                        terminal = self._record_turn_usage(payload, name, turn_usage)
                        if terminal:
                            completed_turn_count += 1
                        yield UpstreamStreamEvent(event_name=name, payload=payload)
                        turn = _turn_from_event(payload) or {}
                        root_terminal_seen = bool(
                            terminal
                            and name in ROOT_TERMINAL_EVENTS
                            and turn.get("subagent_id") in (None, "")
                        )
                        if root_terminal_seen:
                            root_terminal_event = name

            if not root_terminal_seen:
                raise ApiError(
                    502,
                    "openai_stream_ended_before_turn_terminal",
                    "OpenAI closed the event stream before the root agent turn reached a terminal state.",
                    {"recovery": "Retrieve the session and turn before retrying the same logical message."},
                )

            completed_turn_count = len(turn_usage)
            missing_count = sum(
                not isinstance(report.get("usage"), dict)
                for report in turn_usage.values()
            )
            raw_usage = {
                "provider": "openai_agents_api",
                "billing_status": "pending_worker_processing",
                "cost_estimation_method": "reported_agents_api_turn_tokens",
                "pricing_model": agent_config.model or "",
                "raw_turn_usage_reports": list(turn_usage.values()),
                "completed_agent_turn_count": completed_turn_count,
                "missing_usage_turn_count": missing_count,
            }
            yield UpstreamStreamEvent(
                event_name="middleware.usage.raw",
                payload={"type": "middleware.usage.raw", "usage": raw_usage},
            )
            if root_terminal_event in {
                "agent.session.turn.failed",
                "agent.session.turn.cancelled",
            }:
                raise ApiError(
                    502,
                    "openai_agent_turn_failed" if root_terminal_event.endswith("failed") else "openai_agent_turn_cancelled",
                    "The OpenAI managed agent did not complete this turn successfully.",
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ApiError(
                504,
                "openai_managed_connect_timeout",
                "Could not connect to the OpenAI Agents API.",
                {"timeout_type": "connect", "detail": str(exc)},
            ) from exc
        except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
            raise ApiError(
                504,
                "openai_managed_read_timeout",
                "The OpenAI managed agent exceeded the stream idle timeout.",
                {"timeout_type": "read", "detail": str(exc)},
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                502,
                "openai_managed_unreachable",
                "The OpenAI Agents API stream could not be reached.",
                {"timeout_type": "connect", "detail": str(exc)},
            ) from exc

    @staticmethod
    def _record_turn_usage(
        payload: dict[str, Any],
        event_name: str | None,
        turn_usage: dict[str, dict[str, Any]],
    ) -> bool:
        if event_name not in ROOT_TERMINAL_EVENTS:
            return False
        turn = _turn_from_event(payload)
        if turn is None:
            return False
        turn_id = str(turn.get("id") or turn.get("turn_id") or "").strip()
        if not turn_id:
            subagent_id = str(turn.get("subagent_id") or "root")
            turn_id = f"{subagent_id}:{len(turn_usage)}"
        usage = turn.get("usage")
        turn_usage[turn_id] = {
            "turn_id": turn_id,
            "subagent_id": turn.get("subagent_id"),
            "usage": usage if isinstance(usage, dict) else None,
        }
        return True

    @staticmethod
    def _parse_event(
        event_name: str | None,
        data_lines: list[str],
    ) -> tuple[dict[str, Any], str | None] | None:
        raw = "\n".join(data_lines).strip()
        if not raw or raw == "[DONE]":
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        name = _event_name(event_name, payload)
        return payload, name

    def extract_text_fragments(self, event_payload: dict[str, object]) -> list[str]:
        fragments: list[str] = []
        event_type = str(event_payload.get("type", ""))
        if event_type.endswith("output_text.delta"):
            delta = event_payload.get("delta")
            if isinstance(delta, str) and delta:
                fragments.append(delta)
        elif event_type.endswith("output_text.done"):
            text = event_payload.get("text")
            if isinstance(text, str) and text:
                fragments.append(text)
        for key in ("text", "output_text"):
            value = event_payload.get(key)
            if isinstance(value, str) and value and value not in fragments:
                fragments.append(value)
        delta = event_payload.get("delta")
        if isinstance(delta, dict):
            value = delta.get("text") or delta.get("output_text")
            if isinstance(value, str) and value:
                fragments.append(value)
        return fragments


async def get_openai_managed_client() -> OpenAIManagedClient:
    global _client_singleton
    if _client_singleton is None:
        async with _client_lock:
            if _client_singleton is None:
                settings = get_settings()
                _client_singleton = OpenAIManagedClient(
                    connect_timeout_seconds=settings.upstream_connect_timeout_seconds,
                    read_timeout_seconds=settings.upstream_read_timeout_seconds,
                    max_connections=settings.http_max_connections,
                    max_keepalive_connections=settings.http_max_keepalive_connections,
                    keepalive_expiry_seconds=settings.http_keepalive_expiry_seconds,
                )
    return _client_singleton


async def close_openai_managed_client() -> None:
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.close()
        _client_singleton = None
