"""Tests for daimon.core.pricing — BILL-02."""

from __future__ import annotations

import dataclasses
import math
import re

import httpx
import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.pricing import (
    AGENT_MODEL_PRICING,
    AGENT_PRICING_CHECKED_ON,
    AGENT_PRICING_SOURCE,
    MODEL_PRICING,
    TOOL_MODEL_PRICING,
    ModelRates,
    cost_of,
    cost_of_requests,
    format_cost,
)


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
    assert MODEL_PRICING["claude-opus-5-5"] == ModelRates(
        input=4.0, output=20.0, cache_write=5.0, cache_read=0.20
    ), "opus 5.5 must be metered at the published standard five-minute cache rates"
    assert MODEL_PRICING["claude-sonnet-5-5"] == ModelRates(
        input=2.0, output=10.0, cache_write=2.50, cache_read=0.10
    ), "sonnet 5.5 cache reads fell to $0.10 on 2026-10-07"
    assert "claude-opus-5" in MODEL_PRICING, "opus 5 must be priced and selectable"
    assert "claude-opus-4-8" in MODEL_PRICING, "opus 4.8 must be priced and selectable"
    assert "claude-opus-4-7" in MODEL_PRICING, "opus 4.7 must be priced"
    assert "claude-sonnet-4-6" in MODEL_PRICING, "sonnet 4.6 must be priced"
    assert "claude-haiku-4-5" in MODEL_PRICING, "haiku 4.5 must be priced"
    for key, rates in MODEL_PRICING.items():
        assert isinstance(rates, ModelRates), f"{key} must hold a ModelRates instance"


def test_sonnet_5_and_opus_4_7_are_metered_at_list_price() -> None:
    """Both rows once overcharged: Sonnet 5 at its withdrawn $3/$15, Opus 4.7 at Opus 4.1's $15/$75.

    Reports read this table as provider cost, so margin belongs in the markup setting.
    """
    assert MODEL_PRICING["claude-sonnet-5"] == ModelRates(
        input=2.0, output=10.0, cache_write=2.50, cache_read=0.20
    ), "sonnet 5's $2/$10 launch price became its standard price"
    assert MODEL_PRICING["claude-opus-4-7"] == ModelRates(
        input=5.0, output=25.0, cache_write=6.25, cache_read=0.50
    ), "opus 4.7 is priced like opus 4.8"


def test_allowed_model_ids_holds_agent_models_only() -> None:
    assert "claude-opus-5-5" in ALLOWED_MODEL_IDS, "opus 5.5 must be selectable"
    assert "claude-sonnet-5-5" in ALLOWED_MODEL_IDS, "sonnet 5.5 must be selectable"
    assert "claude-opus-5" in ALLOWED_MODEL_IDS, "opus 5 must be selectable"
    for model_id in TOOL_MODEL_PRICING:
        assert model_id not in ALLOWED_MODEL_IDS, (
            f"{model_id} is pinned by a tool and must not be selectable as an agent model"
        )
        assert model_id in MODEL_PRICING, f"{model_id} must still be priced for cost lookups"


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


# The published list price of every agent model, as read from
# AGENT_PRICING_SOURCE on the date below. Changing a row in pricing.py without
# changing it here fails, so a price edit always comes with a fresh check.
_PUBLISHED_AGENT_PRICES_2026_10_10: dict[str, ModelRates] = {
    "claude-opus-5-5": ModelRates(input=4.0, output=20.0, cache_write=5.0, cache_read=0.20),
    "claude-opus-5": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-8": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-7": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-sonnet-5-5": ModelRates(input=2.0, output=10.0, cache_write=2.50, cache_read=0.10),
    "claude-sonnet-5": ModelRates(input=2.0, output=10.0, cache_write=2.50, cache_read=0.20),
    "claude-sonnet-4-6": ModelRates(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    "claude-haiku-4-5": ModelRates(input=1.0, output=5.0, cache_write=1.25, cache_read=0.10),
    "claude-haiku-5-5": ModelRates(
        input=0.10,
        output=0.50,
        cache_write=0.125,
        cache_read=0.01,
        long_context_over=100_000,
        long_context=ModelRates(input=0.50, output=2.50, cache_write=0.625, cache_read=0.05),
    ),
}


def test_agent_rows_match_the_dated_price_list() -> None:
    assert AGENT_PRICING_CHECKED_ON == "2026-10-10", (
        "re-check every row against the pricing page, then move this date and the table name"
    )
    assert AGENT_MODEL_PRICING == _PUBLISHED_AGENT_PRICES_2026_10_10, (
        "an agent price changed: check it against AGENT_PRICING_SOURCE and update both tables"
    )


def _display_name(model_id: str) -> str:
    """`claude-sonnet-5-5` -> `Claude Sonnet 5.5`, the pricing page's row label."""
    _, family, *version = model_id.split("-")
    return f"Claude {family.capitalize()} {'.'.join(version)}"


def _money(cell: str) -> float:
    match = re.search(r"\$([0-9.]+)", cell)
    assert match is not None, f"no dollar amount in pricing cell {cell!r}"
    return float(match.group(1))


def _published_agent_prices(page: str) -> dict[str, ModelRates]:
    published: dict[str, ModelRates] = {}
    long_context: dict[str, tuple[int, ModelRates]] = {}
    for line in page.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Name, input, 5m write, 1h write, cache read, output.
        if len(cells) != 6 or not cells[1].startswith("$"):
            continue
        tier = re.fullmatch(r"(.+) \(for prompts (up to|over) ([\d,]+) tokens\)", cells[0])
        name = tier[1] if tier else cells[0]
        rates = ModelRates(
            input=_money(cells[1]),
            output=_money(cells[5]),
            cache_write=_money(cells[2]),
            cache_read=_money(cells[4]),
        )
        if tier:
            threshold = int(tier[3].replace(",", ""))
            if tier[2] == "over":
                long_context.setdefault(name, (threshold, rates))
                continue
            rates = dataclasses.replace(rates, long_context_over=threshold)
        published.setdefault(name, rates)
    for name, (threshold, rates) in long_context.items():
        assert published[name].long_context_over == threshold, "tier boundaries must agree"
        published[name] = dataclasses.replace(published[name], long_context=rates)
    return published


def test_live_price_parser_retains_both_tiers_and_the_boundary() -> None:
    page = """
| Claude Haiku 5.5 (for prompts up to 100,000 tokens) | $0.10 / MTok | $0.125 / MTok | $0.20 / MTok | $0.01 / MTok | $0.50 / MTok |
| Claude Haiku 5.5 (for prompts over 100,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok | $0.05 / MTok | $2.50 / MTok |
"""
    expected = AGENT_MODEL_PRICING["claude-haiku-5-5"]
    assert _published_agent_prices(page)["Claude Haiku 5.5"] == expected
    changed_boundary = page.replace("100,000", "200,000")
    assert _published_agent_prices(changed_boundary)["Claude Haiku 5.5"] != expected
    changed_long_price = page.replace("$0.625", "$0.75")
    assert _published_agent_prices(changed_long_price)["Claude Haiku 5.5"] != expected


@pytest.mark.contract
def test_agent_rows_match_the_live_pricing_page() -> None:
    """Public HTTP only: compare all rates, both tiers and the prompt boundary."""
    response = httpx.get(f"{AGENT_PRICING_SOURCE}.md", follow_redirects=True, timeout=30)
    response.raise_for_status()
    published = _published_agent_prices(response.text)
    for model_id, rates in AGENT_MODEL_PRICING.items():
        name = _display_name(model_id)
        assert published.get(name) == rates, (
            f"{model_id} is billed at {rates}, the pricing page lists {published.get(name)}"
        )


def test_a_dated_snapshot_id_prices_at_its_alias_row() -> None:
    from daimon.core.pricing import MODEL_PRICING

    alias = MODEL_PRICING["claude-haiku-4-5"]
    assert MODEL_PRICING.get("claude-haiku-4-5-20251001") == alias, "snapshot id uses the alias row"
    assert MODEL_PRICING["claude-haiku-4-5-20251001"] == alias


def test_an_unknown_model_still_prices_at_none() -> None:
    from daimon.core.pricing import MODEL_PRICING

    assert MODEL_PRICING.get("claude-nonexistent-20251001") is None
    assert MODEL_PRICING.get("claude-haiku-4-5-2025") is None, (
        "only an 8-digit date suffix falls back"
    )


def _usage(
    input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0
) -> BetaManagedAgentsSpanModelUsage:
    from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
        BetaManagedAgentsSpanModelUsage,
    )

    return BetaManagedAgentsSpanModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )


def test_haiku_5_5_prices_a_short_prompt_at_the_standard_tier() -> None:
    from daimon.core.pricing import MODEL_PRICING, cost_of

    cost = cost_of(_usage(50_000, 1_000, cache_read=50_000), MODEL_PRICING["claude-haiku-5-5"])
    assert cost is not None and math.isclose(
        cost, (50_000 * 0.10 + 1_000 * 0.50 + 50_000 * 0.01) / 1_000_000
    )


def test_haiku_5_5_prices_a_prompt_over_100k_at_the_long_context_tier() -> None:
    from daimon.core.pricing import MODEL_PRICING, cost_of

    cost = cost_of(_usage(60_000, 1_000, cache_read=40_001), MODEL_PRICING["claude-haiku-5-5"])
    assert cost is not None and math.isclose(
        cost, (60_000 * 0.50 + 1_000 * 2.50 + 40_001 * 0.05) / 1_000_000
    )


def test_haiku_5_5_is_selectable_and_gemini_3_8_flash_is_priced() -> None:
    from daimon.core.pricing import MODEL_PRICING

    assert "claude-haiku-5-5" in ALLOWED_MODEL_IDS, (
        "the latest cheap Claude model must be selectable"
    )
    assert MODEL_PRICING["gemini-3.8-flash"] == ModelRates(
        input=0.75, output=3.75, cache_write=0.0, cache_read=0.075
    )


@pytest.mark.parametrize("prompt,expected", [(100_000, 0.0125), (100_001, 0.062500625)])
def test_cache_write_counts_toward_the_haiku_prompt_boundary(prompt: int, expected: float) -> None:
    assert cost_of(
        _usage(0, 0, cache_write=prompt), MODEL_PRICING["claude-haiku-5-5"]
    ) == pytest.approx(expected)


@pytest.mark.parametrize("second,expected", [(60_000, 0.012), (100_001, 0.0560005)])
def test_request_costs_keep_short_and_mixed_tiers(second: int, expected: float) -> None:
    requests = [_usage(60_000, 0), _usage(second, 0)]
    assert cost_of_requests(requests, MODEL_PRICING["claude-haiku-5-5"]) == pytest.approx(expected)


def test_request_costs_omit_unknown_models() -> None:
    assert cost_of_requests([_usage(60_000, 0)], None) is None


def test_output_does_not_count_toward_the_prompt_boundary() -> None:
    assert cost_of(_usage(100_000, 100_001), MODEL_PRICING["claude-haiku-5-5"]) == pytest.approx(
        0.0600005
    )
