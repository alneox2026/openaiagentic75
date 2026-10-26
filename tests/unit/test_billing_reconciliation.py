from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from common.billing import customer_wallet_document_id
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.services import billing_reconciliation
from services.agent_persistence_worker_v3.app.services.billing_reconciliation import (
    BillingReconciliationService,
)
from tests.unit.test_wallet_reservations import FakeClient


@pytest.fixture(autouse=True)
def _reset_worker_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_transaction(client, operation):
    return operation(client.transaction())


def test_expired_reservation_releases_held_credit_when_no_ledger_exists(monkeypatch):
    settings = get_settings()
    client = FakeClient()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 500_000_000,
        "reserved_credit_nanos": 500_000_000,
        "settled_usage_nanos": 0,
    }
    client.documents[(settings.billing_reservations_collection, "turn-1")] = {
        "reservation_id": "turn-1",
        "turn_id": "turn-1",
        "request_id": "req-1",
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "agent_id": "maxima",
        "currency": "USD",
        "reserved_amount_nanos": 500_000_000,
        "status": "reserved",
    }
    monkeypatch.setattr(
        billing_reconciliation,
        "_run_firestore_transaction",
        _run_transaction,
    )
    service = BillingReconciliationService(
        firestore_client_factory=lambda: client,
    )

    released = service._release_expired_reservation_sync(
        client,
        client.documents[(settings.billing_reservations_collection, "turn-1")],
        datetime(2026, 8, 11, 11, 0, tzinfo=timezone.utc),
    )

    assert released is True
    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["available_credit_nanos"] == 1_000_000_000
    assert wallet["reserved_credit_nanos"] == 0
    reservation = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert reservation["status"] == "expired_released"
    transaction = client.documents[(settings.wallet_transactions_collection, "reservation_expired_turn-1")]
    assert transaction["transaction_type"] == "reservation_expiry_release"
    assert transaction["released_amount_nanos"] == 500_000_000


def test_reserved_reservation_query_filters_status_before_applying_batch_limit(
    monkeypatch,
):
    monkeypatch.setenv("BILLING_RECONCILIATION_BATCH_SIZE", "2")
    get_settings.cache_clear()

    class Snapshot:
        def __init__(self, data):
            self.data = data

        def to_dict(self):
            return dict(self.data)

    class Collection:
        def __init__(self, documents):
            self.documents = documents

        def stream(self):
            return list(self.documents)

    class Client:
        def __init__(self, documents):
            self.collection_ref = Collection(documents)

        def collection(self, name):
            return self.collection_ref

    expired = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    documents = [
        Snapshot({"status": "settled", "expires_at": expired})
        for _ in range(5)
    ]
    active = Snapshot({"status": "reserved", "expires_at": expired})
    documents.append(active)
    client = Client(documents)
    service = BillingReconciliationService(firestore_client_factory=lambda: client)

    results = list(service._reserved_reservations(client))

    assert results == [active]


class FakeUsageHydrator:
    def __init__(self, reports):
        self.reports = reports
        self.calls = []

    def hydrate(self, *, session_id, reports):
        self.calls.append((session_id, reports))
        return self.reports


class FakeLedgerRepository:
    def __init__(self):
        self.updates = []

    def update_recovered_usage(self, client, turn_id, usage):
        self.updates.append((turn_id, usage))


class FakeMessagesRepository:
    def __init__(self):
        self.updates = []

    def update_assistant_usage(self, client, *, thread_id, turn_id, usage):
        self.updates.append((thread_id, turn_id, usage))
        return True


class FakeSettlementService:
    def __init__(self):
        self.events = []
        self.late_events = []
        self.collection_events = []
        self.legacy_registrations = []

    def settle_if_required_sync(self, event):
        self.events.append(event)
        return object()

    def settle_late_usage_sync(self, event):
        self.late_events.append(event)
        return SimpleNamespace(status="late_settled")

    def collect_late_usage_shortfall_sync(self, event):
        self.collection_events.append(event)
        return SimpleNamespace(status="late_settled")

    def register_late_usage_recovery_sync(self, reservation, *, now):
        self.legacy_registrations.append((reservation, now))
        return True


class FakeQuery:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def where(self, **kwargs):
        field_filter = kwargs.get("filter")
        field = getattr(field_filter, "field_path", None)
        operator = getattr(field_filter, "op_string", None)
        expected = getattr(field_filter, "value", None)
        if field:
            def matches(snapshot):
                actual = (snapshot.to_dict() or {}).get(field)
                if operator in ("==", "== "):
                    return actual == expected
                if operator == "in":
                    return actual in expected
                if operator == "<=":
                    return actual is not None and actual <= expected
                return True

            self.snapshots = [snapshot for snapshot in self.snapshots if matches(snapshot)]
        return self

    def order_by(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def stream(self):
        return self.snapshots

    def document(self, _document_id):
        if not self.snapshots:
            return FakeDocumentReference({})
        return FakeDocumentReference(self.snapshots[0].data)


class FakeDocumentSnapshot:
    def __init__(self, data):
        self.data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self.data) if self.data is not None else None


class FakeDocumentReference:
    def __init__(self, data):
        self.data = data

    def get(self):
        return FakeDocumentSnapshot(self.data)

    def update(self, updates):
        self.data.update(updates)


class FakeReconciliationClient:
    def __init__(self, settings, reservation, ledger):
        self.settings = settings
        self.reservation = reservation
        self.ledger = ledger

    def collection(self, name):
        if name == self.settings.billing_reservations_collection:
            return FakeQuery([FakeDocumentSnapshot(self.reservation)])
        if name == self.settings.billing_ledger_collection:
            return FakeCollection(self.ledger)
        raise AssertionError(f"Unexpected collection: {name}")


class FakeCollection:
    def __init__(self, data):
        self.data = data

    def document(self, _document_id):
        return FakeDocumentReference(self.data)


def test_reconciler_reprices_turn_when_delayed_openai_usage_arrives(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    get_settings.cache_clear()
    ledger_repository = FakeLedgerRepository()
    messages_repository = FakeMessagesRepository()
    recovered_reports = [
        {
            "turn_id": "provider-turn-1",
            "subagent_id": None,
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens": 100,
                "output_tokens_details": {"reasoning_tokens": 20},
            },
        }
    ]
    hydrator = FakeUsageHydrator(recovered_reports)
    service = BillingReconciliationService(
        firestore_client_factory=lambda: None,
        usage_hydrator=hydrator,
        ledger_repository=ledger_repository,
        messages_repository=messages_repository,
    )
    ledger = {
        "turn_id": "turn-1",
        "session_id": "session-1",
        "thread_id": "thread-1",
        "pricing_model": "gpt-6-luna",
        "usage": {
            "provider": "openai_agents_api",
            "billing_status": "pending_provider_usage",
            "pricing_model": "gpt-6-luna",
            "completed_agent_turn_count": 1,
            "missing_usage_turn_count": 1,
            "usage_reporting_complete": False,
            "estimated_cost_usd": None,
            "raw_turn_usage_reports": [
                {"turn_id": "provider-turn-1", "subagent_id": None, "usage": None}
            ],
        },
    }

    updated = service._recover_openai_usage(object(), ledger)

    assert hydrator.calls[0][0] == "session-1"
    assert updated["usage"]["missing_usage_turn_count"] == 0
    assert updated["usage"]["estimated_cost_usd"] > 0
    assert not service._has_pending_openai_usage(updated)
    assert len(ledger_repository.updates) == 1
    assert messages_repository.updates[0][:2] == ("thread-1", "turn-1")
    get_settings.cache_clear()


def test_reconciler_leaves_missing_usage_pending_before_reservation_expiry(
    monkeypatch,
):
    get_settings.cache_clear()
    settings = get_settings()
    ledger = {
        "event_id": "event-1",
        "turn_id": "turn-1",
        "agent_id": "maxima",
        "uid": "user-1",
        "thread_id": "thread-1",
        "session_id": "session-1",
        "created_at": datetime.now(timezone.utc),
        "estimated_cost_usd": None,
        "usage": {
            "provider": "openai_agents_api",
            "billing_status": "pending_provider_usage",
            "pricing_model": "gpt-6-luna",
            "completed_agent_turn_count": 1,
            "missing_usage_turn_count": 1,
            "usage_reporting_complete": False,
            "estimated_cost_usd": None,
            "raw_turn_usage_reports": [
                {"turn_id": "provider-turn-1", "subagent_id": None, "usage": None}
            ],
        },
    }
    reservation = {
        "status": "reserved",
        "turn_id": "turn-1",
        "expires_at": datetime.now(timezone.utc).replace(year=2027),
        "reservation_id": "turn-1",
        "billing_subject_id": "user-1",
    }
    client = FakeReconciliationClient(settings, reservation, ledger)
    settlement = FakeSettlementService()
    service = BillingReconciliationService(
        firestore_client_factory=lambda: client,
        settlement_service=settlement,
        usage_hydrator=FakeUsageHydrator(ledger["usage"]["raw_turn_usage_reports"]),
        ledger_repository=FakeLedgerRepository(),
        messages_repository=FakeMessagesRepository(),
    )

    result = service.reconcile_expired_sync()

    assert result.scanned_reservations == 1
    assert result.skipped_reservations == 1
    assert settlement.events == []
    assert reservation["usage_reconciliation_status"] == "pending"
    assert reservation["usage_reconciliation_attempt_count"] == 1
    assert reservation["usage_reconciliation_next_attempt_at"] > datetime.now(timezone.utc)
    get_settings.cache_clear()


def test_reconciler_recovers_usage_after_reservation_was_released(monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    ledger = {
        "event_id": "event-1",
        "turn_id": "turn-1",
        "agent_id": "maxima",
        "uid": "user-1",
        "billing_subject_id": "user-1",
        "billing_reservation_id": "turn-1",
        "thread_id": "thread-1",
        "session_id": "session-1",
        "created_at": datetime.now(timezone.utc),
        "estimated_cost_usd_nanos": None,
        "usage": {
            "provider": "openai_agents_api",
            "billing_status": "pending_provider_usage",
            "pricing_model": "gpt-6-luna",
            "completed_agent_turn_count": 1,
            "missing_usage_turn_count": 1,
            "usage_reporting_complete": False,
            "estimated_cost_usd": None,
            "raw_turn_usage_reports": [
                {"turn_id": "provider-turn-1", "subagent_id": None, "usage": None}
            ],
        },
    }
    reservation = {
        "status": "late_usage_pending",
        "turn_id": "turn-1",
        "reservation_id": "turn-1",
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
        "agent_id": "maxima",
        "usage_recovery_hold_registered": True,
        "usage_reconciliation_next_attempt_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "usage_reconciliation_deadline_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
    }
    client = FakeReconciliationClient(settings, reservation, ledger)
    recovered_reports = [
        {
            "turn_id": "provider-turn-1",
            "subagent_id": None,
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens": 100,
                "output_tokens_details": {"reasoning_tokens": 20},
            },
        }
    ]
    settlement = FakeSettlementService()
    ledger_repository = FakeLedgerRepository()
    service = BillingReconciliationService(
        firestore_client_factory=lambda: client,
        settlement_service=settlement,
        usage_hydrator=FakeUsageHydrator(recovered_reports),
        ledger_repository=ledger_repository,
        messages_repository=FakeMessagesRepository(),
    )

    result = service.reconcile_expired_sync()

    assert result.settled_reservations == 1
    assert settlement.late_events[0].turn_id == "turn-1"
    assert ledger_repository.updates[0][1]["missing_usage_turn_count"] == 0
    get_settings.cache_clear()


def test_legacy_unpriced_turn_without_ledger_is_blocked_for_manual_review(monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    reservation = {
        "status": "unpriced_released",
        "turn_id": "turn-legacy",
        "reservation_id": "turn-legacy",
        "billing_subject_id": "user-1",
        "owner_uid": "user-1",
    }
    client = FakeReconciliationClient(settings, reservation, None)
    settlement = FakeSettlementService()
    service = BillingReconciliationService(
        firestore_client_factory=lambda: client,
        settlement_service=settlement,
    )

    result = service.reconcile_expired_sync()

    assert result.scanned_reservations == 1
    assert result.skipped_reservations == 1
    assert len(settlement.legacy_registrations) == 1
    assert reservation["status"] == "usage_recovery_manual_review"
    assert reservation["usage_reconciliation_manual_review_reason"] == "late_ledger_missing"
    get_settings.cache_clear()

