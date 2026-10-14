"""List-price previews reuse the admitted tariff, without a financial cap."""

from datetime import date
from decimal import Decimal, localcontext

import pytest
from daimon.core.pricing import (
    AGENT_MODEL_PRICING,
    MODEL_PRICING,
    ModelRates,
    ProviderPrice,
    list_price_preview,
)


def test_actual_model_and_existing_prompt_tiers_are_preserved() -> None:
    preview = list_price_preview("claude-haiku-5-5", MODEL_PRICING.get("claude-haiku-5-5"))
    assert (
        "prompt <= 100,000: input $0.1, output $0.5, cache-read $0.01, cache-write $0.125"
        in preview
    )
    assert (
        "prompt > 100,000: input $0.5, output $2.5, cache-read $0.05, cache-write $0.625" in preview
    )
    assert "5-minute rate" in preview
    assert "checked 2026-10-10" in preview
    opus = list_price_preview("claude-opus-5-5", AGENT_MODEL_PRICING["claude-opus-5-5"])
    assert "input $4, output $20, cache-read $0.2, cache-write $5" in opus
    assert "haiku" not in opus
    assert "prompt >" not in opus


def test_existing_snapshot_alias_prices_the_same_model() -> None:
    model = "claude-haiku-5-5-20261010"
    preview = list_price_preview(model, MODEL_PRICING.get(model))
    assert preview.startswith(model + ": list price")
    assert "cache-write $0.625" in preview


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")]
)
def test_injected_native_tariff_is_exact_and_context_independent(provider: str, model: str) -> None:
    # Mock tariffs: these assertions prove formatting, not a new provider table.
    rates = ProviderPrice(
        provider,
        model,
        date(2026, 10, 12),
        Decimal("0.123456789"),
        Decimal("3.75"),
        Decimal("0.075"),
    )
    with localcontext() as ctx:
        ctx.prec = 6
        preview = list_price_preview(model, rates)
    assert (
        "input $0.123456789, output $3.75, cache-read $0.075, cache-write inapplicable" in preview
    )
    assert "checked 2026-10-12; supplied request tier" in preview
    assert "Worst-case per turn: unknown" in preview
    assert "Excludes billing markup, hosted runtime, external tools and cache storage" in preview


def test_missing_tariff_is_not_free_or_a_fallback_model() -> None:
    preview = list_price_preview("unconfigured-model", None)
    assert preview.startswith("unconfigured-model: list price unknown.")
    assert "$" not in preview
    assert "Worst-case per turn: unknown" in preview


def test_unverified_tariff_cannot_be_presented_as_a_verified_list_price() -> None:
    rates = ProviderPrice(
        "mock",
        "model",
        date(2026, 10, 12),
        Decimal("1"),
        Decimal("2"),
        Decimal("0"),
        verified=False,
    )
    preview = list_price_preview("model", rates)
    assert "list price unverified" in preview
    assert "$" not in preview


def test_price_for_a_different_model_refuses() -> None:
    rates = ProviderPrice(
        "mock", "model", date(2026, 10, 12), Decimal("1"), Decimal("2"), Decimal("0")
    )
    with pytest.raises(ValueError, match="pricing identity"):
        list_price_preview("other-model", rates)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.0])
def test_invalid_legacy_rates_refuse(invalid: float) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        list_price_preview("model", ModelRates(invalid, 1, 1, 1))
