"""Best-effort, token-based billing estimates for OpenAI Managed Agents.

Agents API usage is reported at the session-turn level and does not expose
cache-write counts. This module charges only the observable model-token
categories, records missing-detail warnings, and never labels the result as an
exact OpenAI invoice amount.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

import yaml


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def has_complete_turn_token_counts(usage: Any) -> bool:
    """Whether one Agents API turn report has both billable token totals."""
    return (
        isinstance(usage, dict)
        and _int(usage.get("input_tokens")) is not None
        and _int(usage.get("output_tokens")) is not None
    )


def _catalog_path() -> Path:
    configured = os.getenv("BILLING_CATALOG_PATH")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"BILLING_CATALOG_PATH is not a file: {path}")
        return path
    filename = (
        "billing.prod.yaml"
        if os.getenv("DEPLOYMENT_ENVIRONMENT", "development").lower() == "production"
        else "billing.test.yaml"
    )
    candidates = (
        Path("/app/config") / filename,
        Path(__file__).resolve().parents[1] / "config" / filename,
        Path("config") / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(f"Could not find required pricing catalog {filename}.")


@lru_cache(maxsize=4)
def _load_catalog(path_string: str) -> dict[str, dict[str, Any]]:
    path = Path(path_string)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Could not read pricing catalog {path}.") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RuntimeError(f"Pricing catalog {path} must use schema_version: 1.")
    if str(raw.get("currency", "")).upper() != "USD":
        raise RuntimeError(f"Pricing catalog {path} must specify currency: USD.")
    models = raw.get("models")
    if not isinstance(models, dict) or not models:
        raise RuntimeError(f"Pricing catalog {path} needs a non-empty models mapping.")
    return {str(model).strip().lower(): rates for model, rates in models.items() if isinstance(rates, dict)}


def _pricing(model_name: str) -> tuple[str, dict[str, Any]]:
    model = model_name.strip().lower()
    if model.startswith("openai/"):
        model = model[len("openai/") :]
    rates = _load_catalog(str(_catalog_path())).get(model)
    if rates is None:
        raise ValueError(f"No explicit billing catalog entry for OpenAI model '{model}'.")
    required = (
        "pricing_version",
        "context_threshold_tokens",
        "input_usd_per_million",
        "input_cached_usd_per_million",
        "input_cache_write_usd_per_million",
        "output_usd_per_million",
    )
    if any(key not in rates for key in required):
        raise ValueError(f"Pricing catalog entry for '{model}' is incomplete.")
    return model, rates


def aggregate_turn_usage(
    reports: list[dict[str, Any]],
    *,
    model_name: str,
    completed_turn_count: int,
    missing_usage_turn_count: int,
) -> dict[str, Any]:
    """Sum unique completed root/subagent turn reports before pricing once."""
    if not reports and completed_turn_count:
        return {
            "pricing_model": model_name,
            "cost_estimation_method": "reported_agents_api_turn_tokens",
            "estimated_cost_usd": None,
            "estimated_cost_complete": False,
            "usage_reporting_complete": False,
            "completed_agent_turn_count": completed_turn_count,
            "missing_usage_turn_count": missing_usage_turn_count,
            "pricing_warnings": [
                "agents_api_turn_usage_unavailable",
                "no_token_estimate_created_without_provider_usage",
            ],
        }
    input_tokens = output_tokens = cached_tokens = reasoning_tokens = 0
    token_counts_reported = True
    cached_count_reported = True
    cache_write_tokens = 0
    cache_write_reported = True
    for report in reports:
        reported_input = _int(report.get("input_tokens"))
        reported_output = _int(report.get("output_tokens"))
        if reported_input is not None:
            input_tokens += reported_input
        else:
            token_counts_reported = False
        if reported_output is not None:
            output_tokens += reported_output
        else:
            token_counts_reported = False
        input_details = report.get("input_tokens_details")
        cached = _int(input_details.get("cached_tokens")) if isinstance(input_details, dict) else None
        if cached is None:
            cached = _int(report.get("cached_tokens"))
        if cached is None:
            cached_count_reported = False
        else:
            cached_tokens += cached
        output_details = report.get("output_tokens_details")
        reasoning = _int(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
        if reasoning is not None:
            reasoning_tokens += reasoning
        write_tokens = _int(report.get("cache_write_tokens"))
        if write_tokens is None:
            cache_write_reported = False
        else:
            cache_write_tokens += write_tokens

    raw_usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "cached_input_reporting_complete": cached_count_reported,
        "cache_write_reporting_complete": cache_write_reported,
        "token_counts_reporting_complete": token_counts_reported,
        "usage_reporting_complete": missing_usage_turn_count == 0 and token_counts_reported,
        "completed_agent_turn_count": completed_turn_count,
        "missing_usage_turn_count": missing_usage_turn_count,
    }
    if cache_write_reported:
        raw_usage["cache_write_tokens"] = cache_write_tokens
    return normalize_openai_usage(raw_usage, model_name=model_name)


def normalize_openai_usage(
    raw_usage: dict[str, Any], *, model_name: str | None
) -> dict[str, Any]:
    """Price reported tokens using a versioned local rate card."""
    usage = deepcopy(raw_usage)
    if not model_name:
        usage["estimated_cost_usd"] = None
        usage["cost_status"] = "unavailable"
        return usage

    input_tokens = _int(raw_usage.get("input_tokens"))
    output_tokens = _int(raw_usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        usage["estimated_cost_usd"] = None
        usage["cost_status"] = "unavailable"
        usage.setdefault("pricing_warnings", []).append("agents_api_token_usage_missing")
        return usage

    resolved_model, catalog = _pricing(model_name)
    cached_details = raw_usage.get("input_tokens_details")
    cached = _int(cached_details.get("cached_tokens")) if isinstance(cached_details, dict) else None
    if cached is None:
        cached = _int(raw_usage.get("cached_tokens"))
    cached_flag = raw_usage.get("cached_input_reporting_complete")
    cached_reported = (
        cached_flag
        if isinstance(cached_flag, bool)
        else cached is not None
    )
    cached = min(cached or 0, input_tokens)
    cache_write = _int(raw_usage.get("cache_write_tokens"))
    cache_write_reported = (
        cache_write is not None
        or raw_usage.get("cache_write_reporting_complete") is True
    )
    cache_write = min(cache_write or 0, max(input_tokens - cached, 0))
    uncached = max(input_tokens - cached - cache_write, 0)

    threshold = int(catalog["context_threshold_tokens"])
    # Agents API exposes aggregate usage for agent turns, not per model call.
    # Use the short-context rate card as an explicit best-effort estimate;
    # crossing this aggregate threshold is a warning, not proof one call did.
    rates = catalog
    context_tier = "short_estimate"
    warnings: list[str] = []
    if input_tokens > threshold:
        warnings.append("agents_api_usage_aggregated_context_tier_unknown")
    if not cached_reported:
        warnings.append("agents_api_cached_input_count_unreported")
    if not cache_write_reported:
        warnings.append("agents_api_cache_write_tokens_unreported")
    if raw_usage.get("usage_reporting_complete") is False:
        warnings.append("agents_api_turn_usage_incomplete")
    if raw_usage.get("token_counts_reporting_complete") is False:
        warnings.append("agents_api_token_counts_incomplete")
    warnings.extend(
        [
            "estimate_excludes_openai_hosted_environment_and_tool_charges",
            "estimate_uses_reported_turn_usage_not_final_provider_invoice",
        ]
    )

    input_rate = float(rates["input_usd_per_million"])
    cached_rate = float(rates["input_cached_usd_per_million"])
    write_rate = float(rates["input_cache_write_usd_per_million"])
    output_rate = float(rates["output_usd_per_million"])
    costs = {
        "input_uncached": uncached * input_rate / 1_000_000,
        "input_cached": cached * cached_rate / 1_000_000,
        "input_cache_write": cache_write * write_rate / 1_000_000,
        "output_including_reasoning": output_tokens * output_rate / 1_000_000,
    }
    estimate = round(sum(costs.values()), 9)
    output_details = raw_usage.get("output_tokens_details")
    reasoning = (
        _int(output_details.get("reasoning_tokens"))
        if isinstance(output_details, dict)
        else None
    ) or 0
    complete = cached_reported and cache_write_reported and not warnings
    usage.update(
        {
            "pricing_model": resolved_model,
            "pricing_version": str(catalog["pricing_version"]),
            "pricing_unit": "usd_per_1m_tokens",
            "context_tier": context_tier,
            "pricing": {
                "input_text_image_video": input_rate,
                "input_cached": cached_rate,
                "input_cache_write": write_rate,
                "output_including_reasoning": output_rate,
            },
            "token_counts": {
                "prompt_token_count": input_tokens,
                "cached_content_token_count": cached,
                "cache_write_token_count": cache_write,
                "candidates_token_count": output_tokens,
                "thoughts_token_count": reasoning,
                "total_token_count": input_tokens + output_tokens,
            },
            "billable_tokens": {
                "input_uncached": uncached,
                "input_cached": cached,
                "input_cache_write": cache_write,
                "output_including_reasoning": output_tokens,
            },
            "cached_input_tokens": cached,
            "uncached_input_tokens": uncached,
            "cache_write_input_tokens": cache_write,
            "estimated_cost_usd": estimate,
            "estimated_cost_breakdown_usd": {
                key: round(value, 9) for key, value in costs.items()
            },
            "estimated_cost_complete": complete,
            "cost_estimation_method": "reported_agents_api_turn_tokens",
            "pricing_warnings": warnings,
        }
    )
    return usage
