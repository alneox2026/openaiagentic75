import asyncio

from common.schemas import InfrastructureMetering, TurnCompletedEvent
from services.agent_persistence_worker_v3.app.services.persist_turn import (
    PersistTurnService,
)


class Conflict(Exception):
    pass


class FakeBatch:
    def __init__(self, *, raise_conflict: bool = False) -> None:
        self.raise_conflict = raise_conflict
        self.actions = []

    def commit(self) -> None:
        if self.raise_conflict:
            raise Conflict("duplicate")


class FakeClient:
    def __init__(self, *, raise_conflict: bool = False) -> None:
        self.raise_conflict = raise_conflict
        self.batch_instance = FakeBatch(raise_conflict=raise_conflict)

    def batch(self) -> FakeBatch:
        return self.batch_instance


class FakeIdempotencyStore:
    def add_create_to_batch(self, batch, client, event) -> None:
        batch.actions.append(("idempotency", event.event_id))


class FakeBillingLedgerRepository:
    def __init__(self) -> None:
        self.events = []

    def add_create_to_batch(self, batch, client, event) -> None:
        batch.actions.append(("billing_ledger", event.turn_id))
        self.events.append(event)


class FakeThreadsRepository:
    def __init__(self, existing_thread=None, ignored_reason=None) -> None:
        self.existing_thread = existing_thread
        self.ignored_reason = ignored_reason

    def load_existing(self, client, thread_id):
        return self.existing_thread

    def validate_existing_for_turn(self, existing_thread, event):
        return self.ignored_reason

    def add_upsert_to_batch(self, batch, client, event, *, existing_thread=None) -> None:
        batch.actions.append(("thread", event.thread_id))


class FakeMessagesRepository:
    def add_turn_messages_to_batch(self, batch, client, event) -> None:
        batch.actions.append(("messages", event.turn_id))


class FakeSettlementService:
    def __init__(self):
        self.events = []

    def settle_if_required_sync(self, event):
        self.events.append(event)
        return None


def _build_event() -> TurnCompletedEvent:
    return TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
    )


def test_persist_turn_commits_batch_once() -> None:
    client = FakeClient()
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=FakeBillingLedgerRepository(),
        threads_repository=FakeThreadsRepository(),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=lambda: client,
    )

    result = asyncio.run(service.persist(_build_event()))

    assert result.persisted is True
    assert client.batch_instance.actions == [
        ("idempotency", "evt-1"),
        ("billing_ledger", "turn-1"),
        ("thread", "thread-1"),
        ("messages", "turn-1"),
    ]


def test_persist_turn_treats_conflict_as_duplicate() -> None:
    client = FakeClient(raise_conflict=True)
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=FakeBillingLedgerRepository(),
        threads_repository=FakeThreadsRepository(),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=lambda: client,
    )

    result = asyncio.run(service.persist(_build_event()))

    assert result.persisted is False


def test_persist_turn_records_idempotency_only_for_deleted_thread() -> None:
    client = FakeClient()
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=FakeBillingLedgerRepository(),
        threads_repository=FakeThreadsRepository(ignored_reason="thread_deleted"),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=lambda: client,
    )

    result = asyncio.run(service.persist(_build_event()))

    assert result.persisted is False
    assert result.ignored_reason == "thread_deleted"
    assert client.batch_instance.actions == [("idempotency", "evt-1")]


def test_openai_turn_usage_is_priced_in_persistence_worker(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    ledger = FakeBillingLedgerRepository()
    event = _build_event().model_copy(
        update={
            "usage": {
                "provider": "openai_agents_api",
                "billing_status": "pending_worker_processing",
                "pricing_model": "gpt-6-luna",
                "completed_agent_turn_count": 1,
                "missing_usage_turn_count": 0,
                "raw_turn_usage_reports": [
                    {
                        "turn_id": "agent_turn_1",
                        "subagent_id": None,
                        "usage": {
                            "input_tokens": 1000,
                            "input_tokens_details": {"cached_tokens": 200},
                            "output_tokens": 100,
                            "output_tokens_details": {"reasoning_tokens": 20},
                        },
                    }
                ],
            },
            "infrastructure_metering": InfrastructureMetering(
                deployment_environment="development",
                project_id="test-project",
                gateway_region="us-central1",
                billing_subject_id="user-1",
                root_request_id="req-1",
                agent_backend="openai_managed",
                agent_region="global",
                gateway_wall_duration_ms=100,
                assistant_content_bytes=2,
            ),
        }
    )
    client = FakeClient()
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=ledger,
        threads_repository=FakeThreadsRepository(),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=lambda: client,
    )

    asyncio.run(service.persist(event))

    processed_usage = ledger.events[0].usage
    assert processed_usage["billing_status"] == "processed_by_persistence_worker"
    assert processed_usage["token_counts"]["prompt_token_count"] == 1000
    assert processed_usage["token_counts"]["cached_content_token_count"] == 200
    assert processed_usage["estimated_cost_usd"] == 0.000132
    assert processed_usage["raw_turn_usage_reports"][0]["turn_id"] == "agent_turn_1"


def test_missing_openai_usage_does_not_create_a_zero_charge(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    event = _build_event().model_copy(
        update={
            "usage": {
                "pricing_model": "gpt-6-luna",
                "completed_agent_turn_count": 1,
                "raw_turn_usage_reports": [
                    {"turn_id": "agent_turn_1", "subagent_id": None, "usage": None}
                ],
            },
            "infrastructure_metering": InfrastructureMetering(
                deployment_environment="development",
                project_id="test-project",
                gateway_region="us-central1",
                billing_subject_id="user-1",
                root_request_id="req-1",
                agent_backend="openai_managed",
                agent_region="global",
                gateway_wall_duration_ms=100,
                assistant_content_bytes=2,
            ),
        }
    )
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=FakeBillingLedgerRepository(),
        threads_repository=FakeThreadsRepository(),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=FakeClient,
    )

    processed = service._normalize_openai_usage_after_response(event)

    assert processed.usage["estimated_cost_usd"] is None
    assert processed.usage["estimated_cost_complete"] is False
    assert "agents_api_turn_usage_unavailable" in processed.usage["pricing_warnings"]


def test_persist_turn_keeps_reservation_pending_until_openai_usage_arrives(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ledger = FakeBillingLedgerRepository()
    settlement = FakeSettlementService()
    event = _build_event().model_copy(
        update={
            "usage": {
                "provider": "openai_agents_api",
                "pricing_model": "gpt-6-luna",
                "completed_agent_turn_count": 1,
                "raw_turn_usage_reports": [
                    {"turn_id": "agent_turn_1", "subagent_id": None, "usage": None}
                ],
            },
            "infrastructure_metering": InfrastructureMetering(
                deployment_environment="development",
                project_id="test-project",
                gateway_region="us-central1",
                billing_subject_id="user-1",
                root_request_id="req-1",
                agent_backend="openai_managed",
                agent_region="global",
                gateway_wall_duration_ms=100,
                assistant_content_bytes=2,
            ),
        }
    )
    service = PersistTurnService(
        idempotency_store=FakeIdempotencyStore(),
        billing_ledger_repository=ledger,
        billing_settlement_service=settlement,
        threads_repository=FakeThreadsRepository(),
        messages_repository=FakeMessagesRepository(),
        firestore_client_factory=FakeClient,
    )

    result = asyncio.run(service.persist(event))

    assert result.billing_settlement_status == "usage_pending"
    assert settlement.events == []
    assert ledger.events[0].usage["billing_status"] == "pending_provider_usage"
    assert ledger.events[0].usage["estimated_cost_usd"] is None
