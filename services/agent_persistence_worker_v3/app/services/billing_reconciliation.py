"""Reconciliation for reservations whose completed-turn event never arrived."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from common.billing import customer_wallet_document_id, nonnegative_int
from common.firestore import get_transaction_document_snapshot
from common.openai_usage_metadata import (
    aggregate_turn_usage,
    has_complete_turn_token_counts,
)
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)
from services.agent_persistence_worker_v3.app.services.billing_ledger import (
    BillingLedgerRepository,
)
from services.agent_persistence_worker_v3.app.services.firestore_client import (
    get_firestore_client,
)
from services.agent_persistence_worker_v3.app.services.firestore_messages import (
    FirestoreMessagesRepository,
)
from services.agent_persistence_worker_v3.app.services.openai_turn_usage_hydrator import (
    OpenAITurnUsageHydrator,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BillingReconciliationResult:
    scanned_reservations: int
    settled_reservations: int
    released_reservations: int
    skipped_reservations: int


class BillingReconciliationService:
    """Settle finalized ledgers, or release genuinely orphaned expired holds."""

    def __init__(
        self,
        *,
        firestore_client_factory: Callable[[], Any] | None = None,
        settlement_service: BillingSettlementService | None = None,
        usage_hydrator: OpenAITurnUsageHydrator | None = None,
        ledger_repository: BillingLedgerRepository | None = None,
        messages_repository: FirestoreMessagesRepository | None = None,
    ) -> None:
        self._settings = get_settings()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client
        self._settlement_service = settlement_service or BillingSettlementService(
            firestore_client_factory=self._firestore_client_factory,
        )
        # One non-blocking provider lookup per scheduled pass; the original
        # Pub/Sub worker already makes its short post-response retry attempts.
        self._usage_hydrator = usage_hydrator or OpenAITurnUsageHydrator(attempts=1)
        self._ledger_repository = ledger_repository or BillingLedgerRepository()
        self._messages_repository = messages_repository or FirestoreMessagesRepository()

    async def reconcile_expired(self) -> BillingReconciliationResult:
        return await asyncio.to_thread(self.reconcile_expired_sync)

    def reconcile_expired_sync(self) -> BillingReconciliationResult:
        client = self._firestore_client_factory()
        now = datetime.now(timezone.utc)
        reservations = list(self._reserved_reservations(client))
        late_reservations = list(self._late_usage_reservations(client, now))
        legacy_unpriced = list(self._legacy_unpriced_reservations(client))
        scanned = settled = released = skipped = 0
        for reservation_snapshot in reservations:
            scanned += 1
            reservation = reservation_snapshot.to_dict() or {}
            if reservation.get("status") != "reserved":
                skipped += 1
                continue
            expired = _reservation_is_expired(reservation, now)
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            ledger_snapshot = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id).get()
            if ledger_snapshot.exists:
                ledger = ledger_snapshot.to_dict() or {}
                if self._has_pending_openai_usage(ledger):
                    if not expired and not _recovery_is_due(reservation, now):
                        skipped += 1
                        continue
                    ledger = self._recover_openai_usage(client, ledger)
                    if self._has_pending_openai_usage(ledger):
                        if not expired:
                            self._schedule_usage_recovery_retry(
                                client, reservation, now
                            )
                            skipped += 1
                            continue
                        self._schedule_usage_recovery_retry(
                            client, reservation, now
                        )
                        # The wallet hold expires independently of metadata
                        # recovery. Settlement releases the hold and registers
                        # a wallet-level block until usage is recovered/paid.
                        event = _event_from_ledger_and_reservation(
                            ledger,
                            reservation,
                        )
                        settlement = self._settlement_service.settle_if_required_sync(
                            event
                        )
                        if settlement is None:
                            skipped += 1
                        else:
                            settled += 1
                        continue
                event = _event_from_ledger_and_reservation(
                    ledger,
                    reservation,
                )
                settlement = self._settlement_service.settle_if_required_sync(event)
                if settlement is None:
                    skipped += 1
                else:
                    settled += 1
                continue
            if expired and self._release_expired_reservation_sync(client, reservation, now):
                released += 1
            else:
                skipped += 1

        for reservation_snapshot in late_reservations:
            scanned += 1
            reservation = reservation_snapshot.to_dict() or {}
            status = reservation.get("status")
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            event: TurnCompletedEvent | None = None
            ledger: dict[str, Any] = {}
            if status == "late_settled_shortfall":
                event, ledger = self._late_event_and_ledger(client, reservation)
                if event is None:
                    self._mark_manual_review(
                        client, reservation, now, reason="late_ledger_missing"
                    )
                    skipped += 1
                    continue
                result = self._settlement_service.collect_late_usage_shortfall_sync(
                    event
                )
                if result is None or result.status == "late_collection_pending":
                    self._schedule_late_collection_retry(client, reservation, now)
                    skipped += 1
                else:
                    settled += 1
                continue

            if status != "late_usage_pending":
                skipped += 1
                continue
            deadline = _as_utc_or_none(
                reservation.get("usage_reconciliation_deadline_at")
            )
            if deadline is not None and now >= deadline:
                self._mark_manual_review(
                    client, reservation, now, reason="provider_usage_retention_expired"
                )
                skipped += 1
                continue
            if not _recovery_is_due(reservation, now):
                skipped += 1
                continue

            ledger_snapshot = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id).get()
            if not ledger_snapshot.exists:
                self._mark_manual_review(
                    client, reservation, now, reason="late_ledger_missing"
                )
                skipped += 1
                continue
            ledger = ledger_snapshot.to_dict() or {}
            if self._has_pending_openai_usage(ledger):
                ledger = self._recover_openai_usage(client, ledger)
            if self._has_pending_openai_usage(ledger):
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
                continue
            if ledger.get("estimated_cost_usd_nanos") is None:
                self._mark_manual_review(
                    client, reservation, now, reason="recovered_usage_unpriced"
                )
                skipped += 1
                continue
            event = _event_from_ledger_and_reservation(ledger, reservation)
            result = self._settlement_service.settle_late_usage_sync(event)
            if result is None:
                skipped += 1
            elif result.status == "late_settled_shortfall":
                reservation = dict(reservation)
                reservation["status"] = "late_settled_shortfall"
                self._schedule_late_collection_retry(client, reservation, now)
                skipped += 1
            else:
                settled += 1

        for reservation_snapshot in legacy_unpriced:
            reservation = reservation_snapshot.to_dict() or {}
            if reservation.get("status") != "unpriced_released":
                continue
            scanned += 1
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            ledger_snapshot = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id).get()
            register = getattr(
                self._settlement_service,
                "register_late_usage_recovery_sync",
                None,
            )
            registered = (
                register(reservation, now=now)
                if register is not None
                else False
            )
            if registered:
                if not ledger_snapshot.exists:
                    self._mark_manual_review(
                        client,
                        {**reservation, "status": "late_usage_pending"},
                        now,
                        reason="late_ledger_missing",
                    )
                skipped += 1
                continue
            # Keep compatibility with test doubles/older service implementations,
            # but never close a real unpriced reservation without registering its
            # account-level block first.
            self._mark_legacy_unpriced_final(client, reservation, now)
            skipped += 1

        return BillingReconciliationResult(
            scanned_reservations=scanned,
            settled_reservations=settled,
            released_reservations=released,
            skipped_reservations=skipped,
        )

    def _late_usage_reservations(self, client: Any, now: datetime):
        collection = client.collection(self._settings.billing_reservations_collection)
        batch_size = self._settings.billing_reconciliation_batch_size
        statuses = ["late_usage_pending", "late_settled_shortfall"]
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            snapshots = (
                collection.where(filter=FieldFilter("status", "in", statuses))
                .where(
                    filter=FieldFilter(
                        "usage_reconciliation_next_attempt_at", "<=", now
                    )
                )
                .order_by("usage_reconciliation_next_attempt_at")
                .limit(batch_size)
                .stream()
            )
            return [
                snapshot
                for snapshot in snapshots
                if (snapshot.to_dict() or {}).get("status") in statuses
                and _recovery_is_due(snapshot.to_dict() or {}, now)
            ]
        except (AttributeError, ModuleNotFoundError, ImportError):
            candidates = []
            for snapshot in collection.stream():
                reservation = snapshot.to_dict() or {}
                if reservation.get("status") not in statuses:
                    continue
                if _recovery_is_due(reservation, now):
                    due_at = _as_utc_or_none(
                        reservation.get("usage_reconciliation_next_attempt_at")
                    ) or datetime.min.replace(tzinfo=timezone.utc)
                    candidates.append((due_at, snapshot))
            candidates.sort(key=lambda item: item[0])
            return [snapshot for _, snapshot in candidates[:batch_size]]

    def _legacy_unpriced_reservations(self, client: Any):
        collection = client.collection(self._settings.billing_reservations_collection)
        batch_size = self._settings.billing_reconciliation_batch_size
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            return (
                collection.where(filter=FieldFilter("status", "==", "unpriced_released"))
                .order_by("expires_at")
                .limit(batch_size)
                .stream()
            )
        except (AttributeError, ModuleNotFoundError, ImportError):
            candidates = []
            for snapshot in collection.stream():
                reservation = snapshot.to_dict() or {}
                if reservation.get("status") == "unpriced_released":
                    expires_at = _as_utc_or_none(reservation.get("expires_at"))
                    candidates.append(
                        (
                            expires_at or datetime.max.replace(tzinfo=timezone.utc),
                            snapshot,
                        )
                    )
            candidates.sort(key=lambda item: item[0])
            return [snapshot for _, snapshot in candidates[:batch_size]]

    def _schedule_usage_recovery_retry(
        self,
        client: Any,
        reservation: dict[str, Any],
        now: datetime,
    ) -> bool:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        attempts = _nonnegative_count(
            reservation.get("usage_reconciliation_attempt_count"), default=0
        )
        started_at = _as_utc_or_none(
            reservation.get("usage_reconciliation_started_at")
        ) or now
        deadline = _as_utc_or_none(
            reservation.get("usage_reconciliation_deadline_at")
        ) or started_at + timedelta(
            seconds=max(
                1,
                self._settings.billing_usage_reconciliation_retention_seconds,
            )
        )
        if now >= deadline:
            if reservation.get("status") != "reserved":
                self._mark_manual_review(
                    client,
                    reservation,
                    now,
                    reason="provider_usage_retention_expired",
                )
                return False
        initial_delay = max(
            1,
            self._settings.billing_usage_reconciliation_initial_backoff_seconds,
        )
        maximum_delay = max(
            initial_delay,
            self._settings.billing_usage_reconciliation_max_backoff_seconds,
        )
        delay = min(initial_delay * (2 ** min(attempts, 20)), maximum_delay)
        client.collection(self._settings.billing_reservations_collection).document(
            turn_id
        ).update(
            {
                "usage_reconciliation_status": "pending",
                "usage_reconciliation_attempt_count": attempts + 1,
                "usage_reconciliation_started_at": started_at,
                "usage_reconciliation_deadline_at": deadline,
                "usage_reconciliation_next_attempt_at": now
                + timedelta(seconds=delay),
                "updated_at": now,
            }
        )
        return True

    def _schedule_late_collection_retry(
        self,
        client: Any,
        reservation: dict[str, Any],
        now: datetime,
    ) -> None:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        attempts = _nonnegative_count(
            reservation.get("usage_reconciliation_attempt_count"), default=0
        )
        # Collection retries need not call the provider; check again at the
        # configured initial interval so a top-up restores access promptly.
        delay = max(
            1,
            self._settings.billing_usage_reconciliation_initial_backoff_seconds,
        )
        client.collection(self._settings.billing_reservations_collection).document(
            turn_id
        ).update(
            {
                "usage_reconciliation_status": "pending_collection",
                "usage_reconciliation_attempt_count": attempts + 1,
                "usage_reconciliation_next_attempt_at": now
                + timedelta(seconds=delay),
                "updated_at": now,
            }
        )

    def _mark_manual_review(
        self,
        client: Any,
        reservation: dict[str, Any],
        now: datetime,
        *,
        reason: str,
    ) -> None:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        client.collection(self._settings.billing_reservations_collection).document(
            turn_id
        ).update(
            {
                "status": "usage_recovery_manual_review",
                "usage_reconciliation_status": "manual_review",
                "usage_reconciliation_manual_review_reason": reason,
                "usage_reconciliation_manual_review_at": now,
                "usage_reconciliation_next_attempt_at": None,
                "updated_at": now,
            }
        )
        LOGGER.error(
            "OpenAI usage recovery requires manual billing review for turn %s (%s).",
            turn_id,
            reason,
        )

    def _mark_legacy_unpriced_final(
        self,
        client: Any,
        reservation: dict[str, Any],
        now: datetime,
    ) -> None:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        client.collection(self._settings.billing_reservations_collection).document(
            turn_id
        ).update(
            {
                "status": "unpriced_final",
                "usage_reconciliation_status": "not_applicable",
                "usage_reconciliation_next_attempt_at": None,
                "updated_at": now,
            }
        )

    def _late_event_and_ledger(
        self,
        client: Any,
        reservation: dict[str, Any],
    ) -> tuple[TurnCompletedEvent | None, dict[str, Any]]:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        ledger_snapshot = client.collection(
            self._settings.billing_ledger_collection
        ).document(turn_id).get()
        if not ledger_snapshot.exists:
            return None, {}
        ledger = ledger_snapshot.to_dict() or {}
        return _event_from_ledger_and_reservation(ledger, reservation), ledger

    def _reserved_reservations(self, client: Any):
        collection = client.collection(self._settings.billing_reservations_collection)
        batch_size = self._settings.billing_reconciliation_batch_size
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            return (
                collection.where(filter=FieldFilter("status", "==", "reserved"))
                .order_by("expires_at")
                .limit(batch_size)
                .stream()
            )
        except (AttributeError, ModuleNotFoundError, ImportError):
            # Test doubles and older fakes may not implement Firestore queries.
            # Keep the same status filter and expiry ordering before limiting.
            candidates = []
            for snapshot in collection.stream():
                reservation = snapshot.to_dict() or {}
                expires_at = reservation.get("expires_at")
                if reservation.get("status") == "reserved":
                    if isinstance(expires_at, datetime):
                        candidates.append((_as_utc(expires_at), snapshot))
                    else:
                        candidates.append((datetime.max.replace(tzinfo=timezone.utc), snapshot))
            candidates.sort(key=lambda item: item[0])
            return [snapshot for _, snapshot in candidates[:batch_size]]

    def _recover_openai_usage(
        self,
        client: Any,
        ledger: dict[str, Any],
    ) -> dict[str, Any]:
        usage = ledger.get("usage")
        if not isinstance(usage, dict) or not self._has_pending_openai_usage(ledger):
            return ledger
        reports = usage.get("raw_turn_usage_reports")
        if not isinstance(reports, list):
            return self._mark_openai_usage_unpriced_pending(client, ledger, usage)

        session_id = str(ledger.get("session_id") or "").strip()
        if not session_id:
            return self._mark_openai_usage_unpriced_pending(client, ledger, usage)
        normalized_reports = [dict(report) for report in reports if isinstance(report, dict)]
        recovered_reports = self._usage_hydrator.hydrate(
            session_id=session_id,
            reports=normalized_reports,
        )
        token_reports = [
            report["usage"]
            for report in recovered_reports
            if has_complete_turn_token_counts(report.get("usage"))
        ]
        completed_count = _nonnegative_count(
            usage.get("completed_agent_turn_count"),
            default=len(recovered_reports),
        )
        missing_count = sum(
            not has_complete_turn_token_counts(report.get("usage"))
            for report in recovered_reports
        )
        if not recovered_reports and completed_count:
            missing_count = completed_count
        model_name = str(usage.get("pricing_model") or ledger.get("pricing_model") or "").strip()
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
                "raw_turn_usage_reports": recovered_reports,
            }
        )
        if missing_count:
            normalized["known_cost_lower_bound_usd"] = normalized.get(
                "estimated_cost_usd"
            )
            normalized["estimated_cost_usd"] = None
            normalized["estimated_cost_complete"] = False
            normalized["cost_status"] = "unavailable"
        else:
            normalized["cost_status"] = (
                "estimated"
                if normalized.get("estimated_cost_usd") is not None
                else "unavailable"
            )

        should_persist = (
            recovered_reports != reports
            or usage.get("billing_status") != normalized.get("billing_status")
            or usage.get("usage_reporting_complete")
            != normalized.get("usage_reporting_complete")
            or usage.get("missing_usage_turn_count")
            != normalized.get("missing_usage_turn_count")
            or ledger.get("estimated_cost_usd") is not None
            or usage.get("estimated_cost_usd") != normalized.get("estimated_cost_usd")
        )
        if not should_persist:
            return ledger

        turn_id = _required_string(ledger.get("turn_id"), "turn_id")
        thread_id = str(ledger.get("thread_id") or "").strip()
        if thread_id:
            self._messages_repository.update_assistant_usage(
                client,
                thread_id=thread_id,
                turn_id=turn_id,
                usage=normalized,
            )
        self._ledger_repository.update_recovered_usage(client, turn_id, normalized)
        updated = dict(ledger)
        updated["usage"] = normalized
        updated["estimated_cost_usd"] = normalized.get("estimated_cost_usd")
        updated["estimated_cost_usd_nanos"] = _cost_usd_nanos(
            normalized.get("estimated_cost_usd")
        )
        updated["cost_status"] = normalized.get("cost_status", "unavailable")
        return updated

    def _mark_openai_usage_unpriced_pending(
        self,
        client: Any,
        ledger: dict[str, Any],
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(usage)
        normalized["known_cost_lower_bound_usd"] = normalized.get(
            "estimated_cost_usd"
        )
        normalized["estimated_cost_usd"] = None
        normalized["estimated_cost_complete"] = False
        normalized["usage_reporting_complete"] = False
        normalized["billing_status"] = "pending_provider_usage"
        normalized["cost_status"] = "unavailable"
        if (
            ledger.get("estimated_cost_usd") is None
            and usage.get("estimated_cost_usd") is None
            and usage.get("billing_status") == "pending_provider_usage"
        ):
            return ledger
        turn_id = _required_string(ledger.get("turn_id"), "turn_id")
        thread_id = str(ledger.get("thread_id") or "").strip()
        if thread_id:
            self._messages_repository.update_assistant_usage(
                client,
                thread_id=thread_id,
                turn_id=turn_id,
                usage=normalized,
            )
        self._ledger_repository.update_recovered_usage(client, turn_id, normalized)
        updated = dict(ledger)
        updated["usage"] = normalized
        updated["estimated_cost_usd"] = None
        updated["cost_status"] = "unavailable"
        return updated

    @staticmethod
    def _has_pending_openai_usage(ledger: dict[str, Any]) -> bool:
        usage = ledger.get("usage")
        if not isinstance(usage, dict):
            return False
        provider = usage.get("provider") or ledger.get("provider")
        if provider != "openai_agents_api":
            return False
        missing_count = usage.get("missing_usage_turn_count", 0)
        if isinstance(missing_count, int) and not isinstance(missing_count, bool):
            if missing_count > 0:
                return True
        reports = usage.get("raw_turn_usage_reports")
        if usage.get("usage_reporting_complete") is False:
            return True
        return isinstance(reports, list) and any(
            isinstance(report, dict)
            and not has_complete_turn_token_counts(report.get("usage"))
            for report in reports
        )

    def _release_expired_reservation_sync(
        self,
        client: Any,
        reservation: dict[str, Any],
        released_at: datetime,
    ) -> bool:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        billing_subject_id = _required_string(
            reservation.get("billing_subject_id"),
            "billing_subject_id",
        )
        owner_uid = _required_string(reservation.get("owner_uid"), "owner_uid")
        reservation_ref = client.collection(
            self._settings.billing_reservations_collection
        ).document(turn_id)
        wallet_ref = client.collection(self._settings.wallets_collection).document(
            customer_wallet_document_id(billing_subject_id)
        )
        transaction_ref = client.collection(
            self._settings.wallet_transactions_collection
        ).document(f"reservation_expired_{turn_id}")

        def operation(transaction: Any) -> bool:
            # Keep all reads before writes for Firestore transaction validity.
            current_reservation_snapshot = get_transaction_document_snapshot(transaction, reservation_ref)
            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)
            transaction_snapshot = get_transaction_document_snapshot(transaction, transaction_ref)
            if transaction_snapshot.exists or not current_reservation_snapshot.exists:
                return False
            current_reservation = current_reservation_snapshot.to_dict() or {}
            if current_reservation.get("status") != "reserved":
                return False
            if not wallet_snapshot.exists:
                raise RetryableWorkerError(
                    f"Wallet is missing for expired reservation {turn_id}."
                )
            wallet = wallet_snapshot.to_dict() or {}
            if (
                wallet.get("status") != "active"
                or wallet.get("billing_subject_id") != billing_subject_id
                or wallet.get("owner_uid") != owner_uid
            ):
                raise RetryableWorkerError(
                    f"Wallet does not match expired reservation {turn_id}."
                )
            try:
                reserved_amount_nanos = nonnegative_int(
                    current_reservation.get("reserved_amount_nanos"),
                    field_name="reserved_amount_nanos",
                )
                available_credit_nanos = int(wallet.get("available_credit_nanos", 0))
                reserved_credit_nanos = nonnegative_int(
                    wallet.get("reserved_credit_nanos"),
                    field_name="reserved_credit_nanos",
                )
            except ValueError as exc:
                raise RetryableWorkerError(
                    f"Wallet data is invalid for expired reservation {turn_id}."
                ) from exc
            if reserved_credit_nanos < reserved_amount_nanos:
                raise RetryableWorkerError(
                    f"Wallet reserved credit is invalid for expired reservation {turn_id}."
                )
            transaction.create(
                transaction_ref,
                {
                    "schema_version": 1,
                    "transaction_id": f"reservation_expired_{turn_id}",
                    "transaction_type": "reservation_expiry_release",
                    "status": "released",
                    "billing_subject_id": billing_subject_id,
                    "owner_uid": owner_uid,
                    "wallet_document_id": customer_wallet_document_id(billing_subject_id),
                    "currency": current_reservation.get("currency", "USD"),
                    "turn_id": turn_id,
                    "request_id": current_reservation.get("request_id"),
                    "agent_id": current_reservation.get("agent_id"),
                    "reservation_id": turn_id,
                    "reserved_amount_nanos": reserved_amount_nanos,
                    "amount_nanos": 0,
                    "released_amount_nanos": reserved_amount_nanos,
                    "created_at": released_at,
                    "reason": "completed_turn_not_received_before_reservation_expiry",
                },
            )
            transaction.update(
                wallet_ref,
                {
                    "available_credit_nanos": available_credit_nanos + reserved_amount_nanos,
                    "reserved_credit_nanos": reserved_credit_nanos - reserved_amount_nanos,
                    "updated_at": released_at,
                    "last_reservation_release_at": released_at,
                },
            )
            transaction.update(
                reservation_ref,
                {
                    "status": "expired_released",
                    "released_at": released_at,
                    "released_amount_nanos": reserved_amount_nanos,
                    "release_reason": "completed_turn_not_received_before_reservation_expiry",
                },
            )
            return True

        return _run_firestore_transaction(client, operation)


def _event_from_ledger_and_reservation(
    ledger: dict[str, Any],
    reservation: dict[str, Any],
) -> TurnCompletedEvent:
    return TurnCompletedEvent(
        event_id=_required_string(ledger.get("event_id"), "event_id"),
        turn_id=_required_string(ledger.get("turn_id"), "turn_id"),
        agent_id=_required_string(ledger.get("agent_id"), "agent_id"),
        user_id=_required_string(ledger.get("uid"), "uid"),
        thread_id=_required_string(ledger.get("thread_id"), "thread_id"),
        session_id=_required_string(ledger.get("session_id"), "session_id"),
        user_message="",
        assistant_message="",
        created_at=_required_datetime(ledger.get("created_at"), "created_at"),
        metadata={
            "billing": {
                "reservation_id": reservation.get("reservation_id"),
                "billing_subject_id": reservation.get("billing_subject_id"),
            }
        },
    )


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetryableWorkerError(f"Billing reconciliation field {field_name} is required.")
    return value.strip()


def _reservation_is_expired(reservation: dict[str, Any], now: datetime) -> bool:
    expires_at = reservation.get("expires_at")
    return isinstance(expires_at, datetime) and _as_utc(expires_at) <= now


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc_or_none(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return _as_utc(value)


def _recovery_is_due(reservation: dict[str, Any], now: datetime) -> bool:
    next_attempt = _as_utc_or_none(
        reservation.get("usage_reconciliation_next_attempt_at")
    )
    return next_attempt is None or next_attempt <= now


def _nonnegative_count(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return max(0, default)
    return value


def _cost_usd_nanos(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int(
        (amount * Decimal("1000000000")).to_integral_value(rounding=ROUND_HALF_UP)
    )


def _required_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RetryableWorkerError(f"Billing reconciliation field {field_name} is required.")
    return value


def _run_firestore_transaction(client: Any, operation: Callable[[Any], bool]) -> bool:
    from google.cloud import firestore

    transaction = client.transaction()

    @firestore.transactional
    def run(transaction: Any) -> bool:
        return operation(transaction)

    return run(transaction)
