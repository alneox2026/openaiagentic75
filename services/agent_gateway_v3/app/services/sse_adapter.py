"""SSE event formatting helpers."""

from __future__ import annotations

from typing import Any

from common.constants import (
    SSE_EVENT_DONE,
    SSE_EVENT_ERROR,
    SSE_EVENT_METADATA,
    SSE_EVENT_STATUS,
    SSE_EVENT_TOKEN,
)

try:
    import orjson

    def _dumps(payload: dict[str, Any]) -> str:
        return orjson.dumps(payload).decode("utf-8")
except ImportError:  # pragma: no cover
    import json

    def _dumps(payload: dict[str, Any]) -> str:
        return json.dumps(payload)


def format_sse(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {_dumps(payload)}\n\n"


def build_metadata_event(payload: dict[str, Any]) -> str:
    return format_sse(SSE_EVENT_METADATA, payload)


def build_status_event(payload: dict[str, Any]) -> str:
    return format_sse(SSE_EVENT_STATUS, payload)


def build_token_event(text: str) -> str:
    return format_sse(SSE_EVENT_TOKEN, {"text": text})


def build_done_event(payload: dict[str, Any]) -> str:
    return format_sse(SSE_EVENT_DONE, payload)


def build_error_event(code: str, message: str, details: dict[str, Any] | None = None) -> str:
    return format_sse(
        SSE_EVENT_ERROR,
        {
            "code": code,
            "message": message,
            "details": details or {},
        },
    )
