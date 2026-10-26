import httpx

from services.agent_persistence_worker_v3.app.services.openai_turn_usage_hydrator import (
    OpenAITurnUsageHydrator,
)


def test_hydrates_missing_turn_usage_after_response(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    requests = []
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/agents/sessions/session-1/turns/turn-1"
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"id": "turn-1", "usage": None},
            )
        return httpx.Response(
            200,
            json={
                "id": "turn-1",
                "usage": {
                    "input_tokens": 150,
                    "output_tokens": 20,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hydrator = OpenAITurnUsageHydrator(
        http_client=client,
        sleep=sleeps.append,
    )

    reports = hydrator.hydrate(
        session_id="session-1",
        reports=[{"turn_id": "turn-1", "subagent_id": None, "usage": None}],
    )

    assert len(requests) == 2
    assert sleeps == [0.5]
    assert reports[0]["usage"] == {"input_tokens": 150, "output_tokens": 20}
    client.close()


def test_skips_provider_lookup_when_stream_contains_all_usage(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    hydrator = OpenAITurnUsageHydrator()
    reports = [
        {
            "turn_id": "turn-1",
            "subagent_id": None,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
    ]

    assert hydrator.hydrate(session_id="session-1", reports=reports) == reports


def test_uses_subagent_turn_endpoint_for_missing_subagent_usage(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == (
            "/v1/agents/sessions/session-1/subagents/subagent-1/turns/child-turn-1"
        )
        return httpx.Response(
            200,
            json={
                "id": "child-turn-1",
                "usage": {"input_tokens": 50, "output_tokens": 5},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hydrator = OpenAITurnUsageHydrator(http_client=client, sleep=lambda _: None)

    reports = hydrator.hydrate(
        session_id="session-1",
        reports=[
            {
                "turn_id": "child-turn-1",
                "subagent_id": "subagent-1",
                "usage": None,
            }
        ],
    )

    assert len(requests) == 1
    assert reports[0]["usage"] == {"input_tokens": 50, "output_tokens": 5}
    client.close()


def test_falls_back_to_session_turn_list_when_targeted_retrieve_is_unsupported(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/turn-1"):
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "turn-1", "usage": {"input_tokens": 80, "output_tokens": 8}}
                ],
                "has_more": False,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hydrator = OpenAITurnUsageHydrator(http_client=client, sleep=lambda _: None)
    reports = hydrator.hydrate(
        session_id="session-1",
        reports=[{"turn_id": "turn-1", "subagent_id": None, "usage": None}],
    )

    assert len(requests) == 2
    assert requests[1].url.path == "/v1/agents/sessions/session-1/turns"
    assert reports[0]["usage"] == {"input_tokens": 80, "output_tokens": 8}
    client.close()
