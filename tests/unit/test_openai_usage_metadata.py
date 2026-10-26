import pytest

from common.openai_usage_metadata import (
    aggregate_turn_usage,
    normalize_openai_usage,
)


def test_aggregates_root_and_subagent_usage_with_cached_input(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    usage = aggregate_turn_usage(
        [
            {
                "input_tokens": 1000,
                "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens_details": {"reasoning_tokens": 20},
            },
            {
                "input_tokens": 500,
                "output_tokens": 50,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens_details": {"reasoning_tokens": 10},
            },
        ],
        model_name="gpt-6-luna",
        completed_turn_count=2,
        missing_usage_turn_count=0,
    )

    assert usage["token_counts"]["prompt_token_count"] == 1500
    assert usage["token_counts"]["cached_content_token_count"] == 300
    assert usage["token_counts"]["candidates_token_count"] == 150
    assert usage["token_counts"]["thoughts_token_count"] == 30
    assert usage["billable_tokens"] == {
        "input_uncached": 1200,
        "input_cached": 300,
        "input_cache_write": 0,
        "output_including_reasoning": 150,
    }
    assert usage["estimated_cost_usd"] == 0.000198
    assert usage["estimated_cost_complete"] is False
    assert "agents_api_cache_write_tokens_unreported" in usage["pricing_warnings"]


def test_prices_missing_cached_breakdown_as_uncached_and_warns(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    usage = normalize_openai_usage(
        {"input_tokens": 1000, "output_tokens": 100},
        model_name="gpt-6-luna",
    )

    assert usage["cached_input_tokens"] == 0
    assert usage["uncached_input_tokens"] == 1000
    assert usage["estimated_cost_usd"] == 0.00015
    assert "agents_api_cached_input_count_unreported" in usage["pricing_warnings"]


def test_does_not_invent_zero_cost_when_all_turn_usage_is_missing() -> None:
    usage = aggregate_turn_usage(
        [],
        model_name="gpt-6-luna",
        completed_turn_count=1,
        missing_usage_turn_count=1,
    )

    assert usage["estimated_cost_usd"] is None
    assert usage["usage_reporting_complete"] is False
    assert "agents_api_turn_usage_unavailable" in usage["pricing_warnings"]


def test_marks_partial_turn_token_usage_incomplete(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    usage = aggregate_turn_usage(
        [
            {"input_tokens": 100, "output_tokens": 10},
            {"input_tokens": 50},
        ],
        model_name="gpt-6-luna",
        completed_turn_count=2,
        missing_usage_turn_count=0,
    )

    assert usage["estimated_cost_usd"] is not None
    assert usage["usage_reporting_complete"] is False
    assert "agents_api_token_counts_incomplete" in usage["pricing_warnings"]


def test_unknown_model_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    with pytest.raises(ValueError, match="No explicit billing catalog entry"):
        normalize_openai_usage(
            {"input_tokens": 10, "output_tokens": 1},
            model_name="unlisted-model",
        )
