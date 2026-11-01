"""Stored provider usage rows are priced as their settlement priced them."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from daimon.core.pricing import (
    MODEL_PRICING,
    PROVIDER_PRICED_INPUT_LIMITS,
    ProviderPrice,
    provider_cost_of,
    provider_cost_of_bucket,
    provider_price_on,
    provider_usage_tokens,
)
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation

ON = date(2026, 10, 25)


def _observation(price: ProviderPrice, **counts: int | None) -> UsageObservation:
    fields: dict[str, object] = dict(
        id="usage",
        revision=1,
        session=ResourceRef(
            provider=price.provider,
            kind="session",
            id="session",
            tenant_id="tenant",
            account_scope_id="account",
        ),
        model=ModelRef(provider=price.provider, id=price.model),
        grain="turn",
        basis="cumulative",
        observed_at=datetime(ON.year, ON.month, ON.day, 12, tzinfo=UTC),
        completeness="measured",
        native_meter={},
        input_tokens=100_000,
        input_cached_tokens=20_000,
        input_cache_write_tokens=0,
        output_tokens=10_000,
    )
    return UsageObservation.model_validate(fields | counts)


def _price(model: str) -> ProviderPrice:
    price = provider_price_on(model, ON)
    assert price is not None, f"{model} has a tariff on {ON}"
    return price


@pytest.mark.parametrize(
    ("model", "counts"),
    [
        ("gpt-6-luna", {}),
        ("gpt-6-astra", {}),
        ("gpt-6-astra", {"input_tokens": 300_000, "input_cached_tokens": 50_000}),
        ("gpt-6-astra", {"input_cache_write_tokens": None}),
        ("gemini-3.8-flash", {"input_cache_write_tokens": None}),
        ("gemini-4-argon", {"input_tokens": 400_000}),
    ],
)
def test_a_stored_row_costs_what_its_settlement_charged(
    model: str, counts: dict[str, int | None]
) -> None:
    """The row's projected counts price to `provider_cost_of` at $0 infrastructure,
    long-context multipliers and unreported cache writes included."""
    price = _price(model)
    observation = _observation(price, **counts)
    tokens = provider_usage_tokens(observation, price)
    assert tokens is not None, "a measured observation projects to stored counts"
    prompt = (
        tokens.input_tokens + tokens.cache_creation_input_tokens + tokens.cache_read_input_tokens
    )
    limit = PROVIDER_PRICED_INPUT_LIMITS[model]
    charged = provider_cost_of(observation, price, infrastructure_usd=Decimal(0))
    shown = provider_cost_of_bucket(tokens, price, long_prompt=limit is not None and prompt > limit)
    assert shown == charged, f"{model} {counts}: display must equal the settled debit"


def test_a_row_above_an_unpriced_limit_has_no_cost() -> None:
    """Luna has no long-context multipliers; settlement refuses such a turn, so does display."""
    price = _price("gpt-6-luna")
    observation = _observation(price, input_tokens=300_000)
    tokens = provider_usage_tokens(observation, price)
    assert tokens is not None
    assert provider_cost_of(observation, price, infrastructure_usd=Decimal(0)) is None
    assert provider_cost_of_bucket(tokens, price, long_prompt=True) is None, (
        "no long-context price means no cost, as at settlement"
    )


def test_provider_price_on_follows_each_tariffs_dates() -> None:
    assert provider_price_on("gemini-4-argon", date(2026, 10, 21)) is None, (
        "before its checked date a tariff does not apply"
    )
    argon = provider_price_on("gemini-4-argon", date(2026, 11, 30))
    assert argon is not None and argon.input == Decimal("2.00"), "last day of the intro tariff"
    assert provider_price_on("gemini-4-argon", date(2026, 12, 1)) is None, (
        "the intro tariff ends at valid_before"
    )
    assert provider_price_on("claude-opus-4-7", ON) is None, "list-priced models are not here"


def test_provider_and_list_priced_model_ids_are_distinct() -> None:
    """Stored rows name only the model, so one id must never mean two tariffs."""
    assert not set(PROVIDER_PRICED_INPUT_LIMITS) & set(MODEL_PRICING), (
        "a model id is either list-priced or provider-priced"
    )
