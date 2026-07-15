"""Tests for daimon.core.pricing — BILL-02."""

from __future__ import annotations

from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.pricing import MODEL_PRICING, ModelRates, cost_of, format_cost


def test_cost_of_opus_with_cache_returns_expected_usd() -> None:
    rates = MODEL_PRICING["claude-opus-4-7"]
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    cost = cost_of(usage, rates)
    assert cost is not None, "cost_of with valid rates should return a float"
    expected = rates.input + rates.output * 0.5
    assert abs(cost - expected) < 1e-9, (
        f"opus 1M input + 500k output should equal {expected}, got {cost}"
    )


def test_cost_of_unknown_model_returns_none() -> None:
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=100,
        output_tokens=100,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    assert cost_of(usage, None) is None, "missing rates returns None"


def test_format_cost_below_threshold_floor() -> None:
    assert format_cost(0.0001) == "<$0.001", "tiny costs floor to <$0.001 per cma rule"


def test_format_cost_strips_trailing_zeros() -> None:
    assert format_cost(1.50) == "$1.5", "trailing zeros stripped per cma rule"


def test_model_pricing_includes_opus_sonnet_haiku() -> None:
    assert "claude-opus-4-8" in MODEL_PRICING, "opus 4.8 must be priced and selectable"
    assert "claude-opus-4-7" in MODEL_PRICING, "opus 4.7 must be priced (D-17)"
    assert "claude-sonnet-4-6" in MODEL_PRICING, "sonnet 4.6 must be priced (D-17)"
    assert "claude-haiku-4-5" in MODEL_PRICING, "haiku 4.5 must be priced (D-17)"
    for key, rates in MODEL_PRICING.items():
        assert isinstance(rates, ModelRates), f"{key} must hold a ModelRates instance"


def test_cost_of_gemini_tts_model_returns_positive_cost() -> None:
    rates = MODEL_PRICING.get("gemini-3.1-flash-tts-preview")
    assert rates is not None, "gemini-3.1-flash-tts-preview must be a priced model id"
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=0,
        output_tokens=1_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    cost = cost_of(usage, rates)
    assert cost is not None, "cost_of should price gemini-3.1-flash-tts-preview"
    assert cost > 0, "nonzero output tokens should yield a positive cost"


def test_cost_of_gemini_image_model_returns_positive_cost() -> None:
    rates = MODEL_PRICING.get("gemini-3-pro-image-preview")
    assert rates is not None, "gemini-3-pro-image-preview must be a priced model id"
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=0,
        output_tokens=1_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    cost = cost_of(usage, rates)
    assert cost is not None, "cost_of should price gemini-3-pro-image-preview"
    assert cost > 0, "nonzero output tokens should yield a positive cost"


def test_cost_of_gemini_flash_model_returns_positive_cost() -> None:
    rates = MODEL_PRICING.get("gemini-2.5-flash")
    assert rates is not None, "gemini-2.5-flash must be a priced model id"
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=1_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    cost = cost_of(usage, rates)
    assert cost is not None, "cost_of should price gemini-2.5-flash"
    assert cost > 0, "nonzero input tokens should yield a positive cost"


def test_cost_of_gemini_catalog_name_returns_none() -> None:
    """Google's catalog name ('gemini-3-pro-image') is NOT our pinned code constant

    ('gemini-3-pro-image-preview') — guards against keying MODEL_PRICING on the
    wrong string (RESEARCH Pitfall 4).
    """
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=100,
        output_tokens=100,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    assert cost_of(usage, MODEL_PRICING.get("gemini-3-pro-image")) is None, (
        "the catalog name 'gemini-3-pro-image' must not be a MODEL_PRICING key"
    )
