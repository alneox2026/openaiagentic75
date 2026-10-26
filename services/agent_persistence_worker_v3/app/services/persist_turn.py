"""Turn persistence orchestration for Pub/Sub-delivered events."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from common.openai_usage_metadata import (
    aggregate_turn_usage,
    has_complete_turn_token_counts,
)
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError
from services.agent_persistence_worker_v3.app.services.firestore_client import (
    get_firestore_client,
)
from services.agent_persistence_worker_v3.app.services.billing_ledger import (
    BillingLedgerRepository,
)
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)
from services.agent_persistence_worker_v3.app.services.firestore_messages import (
    FirestoreMessagesRepository,
)
from services.agent_persistence_worker_v3.app.services.firestore_threads import (
    FirestoreThreadsRepository,
)
from services.agent_persistence_worker_v3.app.services.idempotency import IdempotencyStore
from services.agent_persistence_worker_v3.app.services.openai_turn_usage_hydrator import (
    OpenAITurnUsageHydrator,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistTurnResult:
    event_id: str
    thread_id: str
    persisted: bool
    ignored_reason: str | None = None
    billing_settlement_status: str | None = None


class PersistTurnService:
    def __init__(
        self,
        idempotency_store: IdempotencyStore | None = None,
        billing_ledger_repository: BillingLedgerRepository | None = None,
        billing_settlement_service: BillingSettlementService | None = None,
        threads_repository: FirestoreThreadsRepository | None = None,
        messages_repository: FirestoreMessagesRepository | None = None,
        firestore_client_factory: Callable[[], Any] | None = None,
        openai_turn_usage_hydrator: OpenAITurnUsageHydrator | None = None,
    ) -> None:
        self.idempotency_store = idempotency_store or IdempotencyStore()
        self.billing_ledger_repository = (
            billing_ledger_repository or BillingLedgerRepository()
        )
        self.billing_settlement_service = (
            billing_settlement_service or BillingSettlementService()
        )
        self.threads_repository = (
            threads_repository or FirestoreThreadsRepository()
        )
        self.messages_repository = (
            messages_repository or FirestoreMessagesRepository()
        )
        self.firestore_client_factory = firestore_client_factory or get_firestore_client
        self.openai_turn_usage_hydrator = (
            openai_turn_usage_hydrator or OpenAITurnUsageHydrator()
        )

    async def persist(self, event: TurnCompletedEvent) -> PersistTurnResult:
        return await asyncio.to_thread(self._persist_sync, event)

    def _persist_sync(self, event: TurnCompletedEvent) -> PersistTurnResult:
        try:
            event = self._normalize_openai_usage_after_response(event)
        except Exception as exc:
            raise RetryableWorkerError(
                f"Failed to calculate OpenAI turn-token billing for {event.turn_id}: {exc}"
            ) from exc
        try:
            client = self.firestore_client_factory()
            existing_thread = self.threads_repository.load_existing(
                client,
                event.thread_id,
            )
            ignored_reason = self.threads_repository.validate_existing_for_turn(
                existing_thread,
                event,
            )
            if ignored_reason:
                return self._persist_idempotency_only(
                    client,
                    event,
                    ignored_reason=ignored_reason,
                )
            batch = client.batch()
            self.idempotency_store.add_create_to_batch(batch, client, event)
            self.billing_ledger_repository.add_create_to_batch(batch, client, event)
            self.threads_repository.add_upsert_to_batch(
                batch,
                client,
                event,
                existing_thread=existing_thread,
            )
            self.messages_repository.add_turn_messages_to_batch(
                batch,
                client,
                event,
            )
            batch.commit()
        except Exception as exc:
            if self._is_conflict_error(exc):
                persisted = False
            else:
                raise RetryableWorkerError(
                    f"Failed to persist turn event {event.event_id}: {exc}"
                ) from exc
        else:
            persisted = True

        if self._has_pending_openai_usage(event):
            # Leave the reservation open. The scheduled reconciler retries the
            # provider lookup and only settles once every agent turn is priced.
            return PersistTurnResult(
                event_id=event.event_id,
                thread_id=event.thread_id,
                persisted=persisted,
                billing_settlement_status="usage_pending",
            )

        try:
            settlement = self.billing_settlement_service.settle_if_required_sync(event)
        except Exception as exc:
            if isinstance(exc, RetryableWorkerError):
                raise
            raise RetryableWorkerError(
                f"Failed to settle billing for turn {event.turn_id}: {exc}"
            ) from exc

        return PersistTurnResult(
            event_id=event.event_id,
            thread_id=event.thread_id,
            persisted=persisted,
            billing_settlement_status=settlement.status if settlement else None,
        )

    def _normalize_openai_usage_after_response(
        self,
        event: TurnCompletedEvent,
    ) -> TurnCompletedEvent:
        metering = event.infrastructure_metering
        usage = event.usage if isinstance(event.usage, dict) else {}
        raw_reports = usage.get("raw_turn_usage_reports")
        is_openai_agents = usage.get("provider") == "openai_agents_api" or (
            metering is not None and metering.agent_backend == "openai_managed"
        )
        if not is_openai_agents:
            return event
        if not isinstance(raw_reports, list):
            raw_reports = []

        reports = self.openai_turn_usage_hydrator.hydrate(
            session_id=event.session_id,
            reports=[report for report in raw_reports if isinstance(report, dict)],
        )
        token_reports = [
            report["usage"]
            for report in reports
            if has_complete_turn_token_counts(report.get("usage"))
        ]
        completed_count = self._nonnegative_count(
            usage.get("completed_agent_turn_count"),
            default=len(reports),
        )
        missing_count = sum(
            not has_complete_turn_token_counts(report.get("usage"))
            for report in reports
        )
        if not reports and completed_count:
            missing_count = completed_count
        model_name = str(usage.get("pricing_model") or "").strip()
        normalized = aggregate_turn_usage(
            token_reports,
            model_name=model_name,
            completed_turn_count=completed_count,
            missing_usage_turn_count=missing_count,
        )
        normalized.update(
            {
                "provider": "openai_agents_api",
                "billing_status": (
                    "pending_provider_usage"
                    if missing_count
                    else "processed_by_persistence_worker"
                ),
                "raw_turn_usage_reports": reports,
            }
        )
        if missing_count:
            # A partial aggregate is useful as a diagnostic lower bound, but
            # must not debit the wallet while any root/subagent turn is absent.
            normalized["known_cost_lower_bound_usd"] = normalized.get(
                "estimated_cost_usd"
            )
            normalized["estimated_cost_usd"] = None
            normalized["estimated_cost_complete"] = False
            normalized["cost_status"] = "unavailable"
            LOGGER.warning(
                "OpenAI token-rate billing has usage missing for %s of %s agent turn(s).",
                missing_count,
                completed_count,
            )
        return event.model_copy(update={"usage": normalized})

    @staticmethod
    def _has_pending_openai_usage(event: TurnCompletedEvent) -> bool:
        usage = event.usage if isinstance(event.usage, dict) else {}
        if usage.get("provider") != "openai_agents_api":
            return False
        missing_count = usage.get("missing_usage_turn_count", 0)
        if isinstance(missing_count, int) and not isinstance(missing_count, bool):
            if missing_count > 0:
                return True
        if usage.get("usage_reporting_complete") is False:
            return True
        reports = usage.get("raw_turn_usage_reports")
        return isinstance(reports, list) and any(
            isinstance(report, dict)
            and not has_complete_turn_token_counts(report.get("usage"))
            for report in reports
        )

    @staticmethod
    def _nonnegative_count(value: Any, *, default: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return max(0, default)
        return value

    def _persist_idempotency_only(
        self,
        client: Any,
        event: TurnCompletedEvent,
        *,
        ignored_reason: str,
    ) -> PersistTurnResult:
        batch = client.batch()
        self.idempotency_store.add_create_to_batch(batch, client, event)
        try:
            batch.commit()
        except Exception as exc:
            if self._is_conflict_error(exc):
                return PersistTurnResult(
                    event_id=event.event_id,
                    thread_id=event.thread_id,
                    persisted=False,
                    ignored_reason=ignored_reason,
                )
            raise RetryableWorkerError(
                f"Failed to record ignored turn event {event.event_id}: {exc}"
            ) from exc
        return PersistTurnResult(
            event_id=event.event_id,
            thread_id=event.thread_id,
            persisted=False,
            ignored_reason=ignored_reason,
        )

    def _is_conflict_error(self, exc: Exception) -> bool:
        if exc.__class__.__name__ == "Conflict":
            return True
        try:
            from google.cloud import exceptions as cloud_exceptions

            return isinstance(exc, cloud_exceptions.Conflict)
        except ModuleNotFoundError:
            return False
