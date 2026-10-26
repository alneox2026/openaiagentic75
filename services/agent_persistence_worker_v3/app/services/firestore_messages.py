"""Message persistence helpers for the worker."""

from __future__ import annotations

import logging
from typing import Any

from common.constants import STATUS_COMPLETED
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings

LOGGER = logging.getLogger(__name__)


class FirestoreMessagesRepository:
    def __init__(self) -> None:
        self._settings = get_settings()

    def _messages_collection(self, client: Any, thread_id: str):
        return (
            client.collection(self._settings.threads_collection)
            .document(thread_id)
            .collection(self._settings.messages_subcollection)
        )

    def add_turn_messages_to_batch(
        self,
        batch: Any,
        client: Any,
        event: TurnCompletedEvent,
    ) -> None:
        messages = self._messages_collection(client, event.thread_id)
        user_doc = messages.document(f"{event.turn_id}_user")
        assistant_doc = messages.document(f"{event.turn_id}_assistant")

        batch.set(
            user_doc,
            {
                "turn_id": event.turn_id,
                "uid": event.user_id,
                "agent_id": event.agent_id,
                "thread_id": event.thread_id,
                "session_id": event.session_id,
                "role": "user",
                "text": event.user_message,
                "created_at": event.created_at,
                "status": STATUS_COMPLETED,
                "metadata": event.metadata,
            },
            merge=True,
        )
        batch.set(
            assistant_doc,
            {
                "turn_id": event.turn_id,
                "uid": event.user_id,
                "agent_id": event.agent_id,
                "thread_id": event.thread_id,
                "session_id": event.session_id,
                "role": "assistant",
                "author": event.agent_id,
                "text": event.assistant_message,
                "created_at": event.created_at,
                "status": STATUS_COMPLETED,
                "usage": event.usage,
                "metadata": event.metadata,
            },
            merge=True,
        )

    def update_assistant_usage(
        self,
        client: Any,
        *,
        thread_id: str,
        turn_id: str,
        usage: dict[str, Any],
    ) -> bool:
        """Refresh the assistant transcript's usage after delayed recovery."""
        assistant_doc = self._messages_collection(client, thread_id).document(
            f"{turn_id}_assistant"
        )
        try:
            assistant_doc.update({"usage": usage})
        except Exception as exc:
            if exc.__class__.__name__ == "NotFound":
                LOGGER.info(
                    "Recovered OpenAI usage for a turn whose assistant message is no longer present."
                )
                return False
            raise
        return True
