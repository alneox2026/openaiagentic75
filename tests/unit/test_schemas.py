from common.schemas import ChatRequest, InfrastructureMetering, TurnCompletedEvent


def test_chat_request_trims_message() -> None:
    payload = ChatRequest(message="  hello  ")
    assert payload.message == "hello"


def test_turn_completed_event_accepts_minimal_payload() -> None:
    event = TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
    )
    assert event.agent_id == "maxima"
    assert event.infrastructure_metering is None


def test_infrastructure_metering_is_explicitly_shadow_only() -> None:
    meter = InfrastructureMetering(
        deployment_environment="development",
        project_id="test-project",
        gateway_region="us-central1",
        billing_subject_id="user-1",
        root_request_id="req-server-generated",
        agent_backend="openai_managed",
        agent_region="global",
        gateway_wall_duration_ms=4200,
        assistant_content_bytes=8,
    )

    assert meter.mode == "shadow"
    assert meter.gateway_wall_duration_ms == 4200
    assert meter.assistant_content_bytes == 8
