"""Correlation metadata helpers for gateway requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import uuid

from common.ids import deterministic_turn_id, new_turn_id


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    turn_id: str
    started_at: datetime
    agent_id: str
    started_monotonic_ns: int = field(default_factory=time.perf_counter_ns)


def build_request_context(
    agent_id: str,
    client_turn_id: str | None = None,
    user_id: str | None = None,
) -> RequestContext:
    started_monotonic_ns = time.perf_counter_ns()
    if client_turn_id and client_turn_id.strip():
        turn_id = deterministic_turn_id(agent_id, client_turn_id.strip(), user_id=(user_id or "").strip())
    else:
        turn_id = new_turn_id()
    return RequestContext(
        request_id=f"req-{uuid.uuid4().hex}",
        turn_id=turn_id,
        started_at=datetime.now(timezone.utc),
        agent_id=agent_id,
        started_monotonic_ns=started_monotonic_ns,
    )
