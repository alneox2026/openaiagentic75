import asyncio
from datetime import datetime, timezone

import pytest

from common.billing import (
    customer_billing_period_document_id,
    customer_wallet_document_id,
)
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)

from tests.unit.test_wallet_reservations import FakeClient


@pytest.fixture(autouse=True)
def _reset_worker_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_transaction(client, operation):
    return operation(client.transaction())


def _enable_worker_billing(monkeypatch):
    monkeypatch.setenv("MONTHLY_SERVICE_FEE_NANOS", "5000000000")
    get_settings.cache_clear()


def _event():
    return TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
        created_at=datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
        metadata={
            "request_id": "req-1",
            "billing": {
                "reservation_id": "turn-1",
                "billing_subject_id": "user-1",
                "reserved_amount_nanos": 500_000_000,
                "currency": "USD",
            },
        },
    )


def _seed_reservation_and_ledger(client):
    settings = get_settings()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "schema_version": 1,
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
    client.documents[(settings.billing_ledger_collection, "turn-1")] = {
        "turn_id": "turn-1",
        "uid": user_id,
        "billing_subject_id": user_id,
        "billing_reservation_id": "turn-1",
        "estimated_cost_usd_nanos": 100_000_000,
        "pricing_model": "gpt-6-luna",
        "pricing_version": "test-price-v1",
    }


def test_settlement_debits_actual_usage_releases_the_rest_and_is_idempotent(monkeypatch):
    _enable_worker_billing(monkeypatch)
    settings = get_settings()
    client = FakeClient()
    _seed_reservation_and_ledger(client)
    service = BillingSettlementService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )

    result = asyncio.run(service.settle_if_required(_event()))
    duplicate_result = asyncio.run(service.settle_if_required(_event()))

    assert result is not None
    assert result.status == "settled"
    assert result.settled_amount_nanos == 100_000_000
    assert result.released_amount_nanos == 400_000_000
    assert duplicate_result == result
    wallet = client.documents[(settings.wallets_collection, customer_wallet_document_id("user-1"))]
    assert wallet["available_credit_nanos"] == 900_000_000
    assert wallet["reserved_credit_nanos"] == 0
    assert wallet["settled_usage_nanos"] == 100_000_000
    wallet_transaction = client.documents[(settings.wallet_transactions_collection, "usage_turn-1")]
    assert wallet_transaction["amount_nanos"] == 100_000_000
    period_id = customer_billing_period_document_id("user-1", "2026-08")
    period = client.documents[(settings.customer_billing_periods_collection, period_id)]
    assert period["usage_estimated_nanos"] == 100_000_000
    assert period["monthly_service_fee_nanos"] == 5_000_000_000
    assert period["monthly_service_fee_status"] == "pending_collection"


def _seed_late_usage_settlement(client, *, available_credit_nanos):
    settings = get_settings()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": available_credit_nanos,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "pending_usage_reconciliation_count": 1,
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
        "status": "late_usage_pending",
        "usage_recovery_hold_registered": True,
    }
    client.documents[(settings.billing_ledger_collection, "turn-1")] = {
        "turn_id": "turn-1",
        "uid": user_id,
        "billing_subject_id": user_id,
        "billing_reservation_id": "turn-1",
        "agent_id": "maxima",
        "estimated_cost_usd_nanos": 100_000_000,
        "pricing_model": "gpt-6-luna",
        "pricing_version": "test-price-v1",
    }
    client.documents[(settings.wallet_transactions_collection, "usage_turn-1")] = {
        "transaction_id": "usage_turn-1",
        "transaction_type": "agent_usage_debit",
        "status": "unpriced_released",
        "amount_nanos": 0,
        "released_amount_nanos": 500_000_000,
        "estimated_cost_nanos": None,
    }
    period_id = customer_billing_period_document_id(user_id, "2026-08")
    client.documents[(settings.customer_billing_periods_collection, period_id)] = {
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "period_key": "2026-08",
        "status": "open",
        "monthly_service_fee_nanos": 5_000_000_000,
        "monthly_service_fee_status": "pending_collection",
        "usage_estimated_nanos": 0,
        "collected_usage_nanos": 0,
        "uncollected_usage_nanos": 0,
        "usage_turn_count": 1,
        "unpriced_turn_count": 1,
    }


def test_late_usage_is_debited_with_separate_idempotent_transaction(monkeypatch):
    _enable_worker_billing(monkeypatch)
    settings = get_settings()
    client = FakeClient()
    _seed_late_usage_settlement(client, available_credit_nanos=500_000_000)
    service = BillingSettlementService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )

    result = service.settle_late_usage_sync(_event())
    duplicate_result = service.settle_late_usage_sync(_event())

    assert result.status == "late_settled"
    assert result.settled_amount_nanos == 100_000_000
    assert duplicate_result == result
    wallet = client.documents[(settings.wallets_collection, customer_wallet_document_id("user-1"))]
    assert wallet["available_credit_nanos"] == 400_000_000
    assert wallet["settled_usage_nanos"] == 100_000_000
    assert wallet["pending_usage_reconciliation_count"] == 0
    late_transaction = client.documents[(settings.wallet_transactions_collection, "late_usage_turn-1")]
    assert late_transaction["transaction_type"] == "agent_usage_late_debit"
    assert late_transaction["amount_nanos"] == 100_000_000
    period_id = customer_billing_period_document_id("user-1", "2026-08")
    period = client.documents[(settings.customer_billing_periods_collection, period_id)]
    assert period["usage_estimated_nanos"] == 100_000_000
    assert period["collected_usage_nanos"] == 100_000_000
    assert period["unpriced_turn_count"] == 0
    assert period["usage_turn_count"] == 1


def test_late_shortfall_blocks_until_topup_is_collected(monkeypatch):
    _enable_worker_billing(monkeypatch)
    settings = get_settings()
    client = FakeClient()
    _seed_late_usage_settlement(client, available_credit_nanos=50_000_000)
    service = BillingSettlementService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )

    first = service.settle_late_usage_sync(_event())

    assert first.status == "late_settled_shortfall"
    assert first.settled_amount_nanos == 50_000_000
    assert first.shortfall_nanos == 50_000_000
    reservation = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert reservation["status"] == "late_settled_shortfall"
    wallet_id = customer_wallet_document_id("user-1")
    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["pending_usage_reconciliation_count"] == 1

    wallet["available_credit_nanos"] += 50_000_000
    collected = service.collect_late_usage_shortfall_sync(_event())

    assert collected.status == "late_settled"
    assert collected.settled_amount_nanos == 50_000_000
    assert wallet["available_credit_nanos"] == 0
    assert wallet["settled_usage_nanos"] == 100_000_000
    assert wallet["pending_usage_reconciliation_count"] == 0
    recovery = client.documents[(settings.wallet_transactions_collection, "late_usage_recovery_turn-1")]
    assert recovery["transaction_type"] == "agent_usage_late_collection"
    assert recovery["amount_nanos"] == 50_000_000
    period_id = customer_billing_period_document_id("user-1", "2026-08")
    period = client.documents[(settings.customer_billing_periods_collection, period_id)]
    assert period["collected_usage_nanos"] == 100_000_000
    assert period["uncollected_usage_nanos"] == 0


def test_legacy_unpriced_reservation_registers_account_billing_hold(monkeypatch):
    _enable_worker_billing(monkeypatch)
    settings = get_settings()
    client = FakeClient()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "status": "active",
        "available_credit_nanos": 1_000_000_000,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
    }
    reservation = {
        "reservation_id": "turn-1",
        "turn_id": "turn-1",
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "status": "unpriced_released",
        "usage_reconciliation_attempt_count": 2,
    }
    client.documents[(settings.billing_reservations_collection, "turn-1")] = reservation
    service = BillingSettlementService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )
    now = datetime(2026, 8, 11, 11, 0, tzinfo=timezone.utc)

    registered = service.register_late_usage_recovery_sync(reservation, now=now)

    assert registered is True
    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["pending_usage_reconciliation_count"] == 1
    current = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert current["status"] == "late_usage_pending"
    assert current["usage_recovery_hold_registered"] is True
    assert current["usage_reconciliation_deadline_at"] == datetime(
        2026, 8, 18, 11, 0, tzinfo=timezone.utc
    )
    assert current["usage_reconciliation_next_attempt_at"] == now

