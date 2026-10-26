"""Best-effort post-response recovery of missing OpenAI Agent turn usage."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx
from common.openai_usage_metadata import has_complete_turn_token_counts


LOGGER = logging.getLogger(__name__)
OPENAI_AGENTS_API_BASE_URL = "https://api.openai.com/v1"
OPENAI_AGENTS_BETA_HEADER = "agents=v1"
MAX_TURN_PAGES_PER_ATTEMPT = 3


class OpenAITurnUsageHydrator:
    """Fill missing streamed usage from the saved session, without blocking chat."""

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        attempts: int = 3,
        timeout_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http_client = http_client
        self._attempts = max(1, attempts)
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep

    def hydrate(
        self,
        *,
        session_id: str,
        reports: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Retry missing usage lookups after the response, returning raw reports."""
        result = [dict(report) for report in reports if isinstance(report, dict)]
        missing_turns = self._missing_turns(result)
        if not missing_turns:
            return result

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            LOGGER.warning(
                "OpenAI turn usage is missing and worker API credentials are not configured."
            )
            return result

        owned_client = self._http_client is None
        client = self._http_client or httpx.Client(
            timeout=httpx.Timeout(self._timeout_seconds, connect=2.0)
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": OPENAI_AGENTS_BETA_HEADER,
            "Accept": "application/json",
        }
        try:
            for attempt in range(self._attempts):
                usage_by_turn: dict[tuple[str, str | None], dict[str, Any]] = {}
                for subagent_id, missing_ids in missing_turns.items():
                    try:
                        usage_by_id = self._fetch_usage(
                            client=client,
                            headers=headers,
                            session_id=session_id,
                            subagent_id=subagent_id,
                            missing_ids=missing_ids,
                        )
                    except (httpx.RequestError, ValueError, TypeError) as exc:
                        LOGGER.warning(
                            "OpenAI turn-usage recovery failed (%s).",
                            type(exc).__name__,
                        )
                        usage_by_id = {}
                    usage_by_turn.update(
                        {(turn_id, subagent_id): usage for turn_id, usage in usage_by_id.items()}
                    )

                if usage_by_turn:
                    result = self._apply_usage(result, usage_by_turn)
                    missing_turns = self._missing_turns(result)
                    if not missing_turns:
                        break
                if attempt + 1 < self._attempts:
                    self._sleep(0.5 * (2**attempt))
        finally:
            if owned_client:
                client.close()

        missing_count = sum(len(turn_ids) for turn_ids in missing_turns.values())
        if missing_count:
            LOGGER.warning(
                "OpenAI usage remains unavailable for %s completed agent turn(s).",
                missing_count,
            )
        return result

    def _fetch_usage(
        self,
        *,
        client: httpx.Client,
        headers: dict[str, str],
        session_id: str,
        subagent_id: str | None,
        missing_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        session_path = quote(session_id, safe="")
        if subagent_id:
            subagent_path = quote(subagent_id, safe="")
            turns_path = f"/agents/sessions/{session_path}/subagents/{subagent_path}/turns"
        else:
            turns_path = f"/agents/sessions/{session_path}/turns"
        url = f"{OPENAI_AGENTS_API_BASE_URL}{turns_path}"

        # A targeted retrieve can expose a newly-populated usage object even
        # when it was absent from an earlier list response. This is also the
        # cheapest lookup for the common single-turn session.
        if len(missing_ids) == 1:
            turn_id = next(iter(missing_ids))
            response = client.get(
                f"{url}/{quote(turn_id, safe='')}",
                headers=headers,
            )
            if response.status_code == 200:
                payload = response.json()
                usage = payload.get("usage") if isinstance(payload, dict) else None
                if isinstance(usage, dict):
                    return {turn_id: usage}
                return found
            if response.status_code not in {400, 404, 405}:
                LOGGER.warning(
                    "OpenAI turn-usage retrieve returned HTTP %s.",
                    response.status_code,
                )
                return found

        params: dict[str, Any] = {"limit": 100, "order": "desc"}
        for _ in range(MAX_TURN_PAGES_PER_ATTEMPT):
            response = client.get(url, headers=headers, params=params)
            if response.status_code != 200:
                LOGGER.warning(
                    "OpenAI turn-usage recovery returned HTTP %s.",
                    response.status_code,
                )
                return found
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                return found
            for turn in data:
                if not isinstance(turn, dict):
                    continue
                turn_id = str(turn.get("id") or turn.get("turn_id") or "").strip()
                usage = turn.get("usage")
                if turn_id in missing_ids and has_complete_turn_token_counts(usage):
                    found[turn_id] = usage
            if missing_ids.issubset(found):
                return found
            if not isinstance(payload, dict) or payload.get("has_more") is not True:
                return found
            last_id = payload.get("last_id")
            if not last_id and data and isinstance(data[-1], dict):
                last_id = data[-1].get("id")
            if not last_id:
                return found
            params["after"] = str(last_id)
        return found

    @staticmethod
    def _missing_turns(
        reports: list[dict[str, Any]],
    ) -> dict[str | None, set[str]]:
        missing: dict[str | None, set[str]] = {}
        for report in reports:
            turn_id = str(report.get("turn_id") or "").strip()
            if not turn_id or has_complete_turn_token_counts(report.get("usage")):
                continue
            raw_subagent_id = str(report.get("subagent_id") or "").strip()
            missing.setdefault(raw_subagent_id or None, set()).add(turn_id)
        return missing

    @staticmethod
    def _apply_usage(
        reports: list[dict[str, Any]],
        usage_by_turn: dict[tuple[str, str | None], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        hydrated: list[dict[str, Any]] = []
        for report in reports:
            turn_id = str(report.get("turn_id") or "").strip()
            raw_subagent_id = str(report.get("subagent_id") or "").strip()
            lookup_key = (turn_id, raw_subagent_id or None)
            if not has_complete_turn_token_counts(report.get("usage")) and lookup_key in usage_by_turn:
                report = {**report, "usage": usage_by_turn[lookup_key]}
            hydrated.append(report)
        return hydrated
