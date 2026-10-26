"""Helpers to build normalized persistence events from gateway turns."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import time
from typing import Any

from common.ids import new_event_id
from common.schemas import (
    AgentConfig,
    ChatRequest,
    InfrastructureMetering,
    ThreadDeleteRequestedEvent,
    ThreadLifecycleRequest,
    TurnCompletedEvent,
)
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.services.request_context import RequestContext


def build_turn_completed_event(
    *,
    request_context: RequestContext,
    agent_config: AgentConfig,
    user_id: str,
    payload: ChatRequest,
    thread_id: str,
    session_id: str,
    assistant_message: str,
    usage: dict[str, Any] | None = None,
    billing_metadata: dict[str, Any] | None = None,
) -> TurnCompletedEvent:
    completed_at = datetime.now(timezone.utc)
    metadata: dict[str, Any] = {
        "request_id": request_context.request_id,
    }
    if payload.client_turn_id:
        metadata["client_turn_id"] = payload.client_turn_id
    if payload.metadata:
        metadata["client_metadata"] = payload.metadata
    if billing_metadata:
        metadata["billing"] = dict(billing_metadata)

    server_billing_subject_id = (
        billing_metadata.get("billing_subject_id")
        if isinstance(billing_metadata, dict)
        else None
    )
    if (
        not isinstance(server_billing_subject_id, str)
        or not server_billing_subject_id.strip()
    ):
        server_billing_subject_id = user_id

    elapsed_ns = time.perf_counter_ns() - request_context.started_monotonic_ns
    settings = get_settings()
    infrastructure_metering = InfrastructureMetering(
        deployment_environment=settings.deployment_environment,
        project_id=settings.project_id,
        gateway_region=settings.region,
        gateway_service_name=os.getenv("K_SERVICE") or None,
        gateway_revision=os.getenv("K_REVISION") or None,
        billing_subject_id=server_billing_subject_id.strip(),
        root_request_id=request_context.request_id,
        agent_backend=agent_config.backend,
        agent_region=agent_config.region,
        gateway_wall_duration_ms=max(0, elapsed_ns // 1_000_000),
        assistant_content_bytes=len(assistant_message.encode("utf-8")),
    )

    return TurnCompletedEvent(
        event_id=new_event_id(),
        turn_id=request_context.turn_id,
        agent_id=agent_config.agent_id,
        user_id=user_id,
        thread_id=thread_id,
        session_id=session_id,
        user_message=payload.message,
        assistant_message=assistant_message,
        created_at=completed_at,
        usage=usage or {},
        infrastructure_metering=infrastructure_metering,
        metadata=metadata,
    )


def build_thread_delete_requested_event(
    *,
    request_context: RequestContext,
    agent_config: AgentConfig,
    user_id: str,
    thread_id: str,
    session_id: str,
    payload: ThreadLifecycleRequest,
) -> ThreadDeleteRequestedEvent:
    metadata: dict[str, Any] = {
        "request_id": request_context.request_id,
    }
    if payload.metadata:
        metadata["client_metadata"] = payload.metadata

    return ThreadDeleteRequestedEvent(
        event_id=new_event_id(),
        agent_id=agent_config.agent_id,
        user_id=user_id,
        thread_id=thread_id,
        session_id=session_id,
        created_at=datetime.now(timezone.utc),
        reason=payload.reason,
        metadata=metadata,
    )
