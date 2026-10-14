"""Mock tariffs prove conservative floors, independent of SDK or invoices."""

from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest
from daimon.core.pricing import ModelRates, ProviderPrice, minimum_cost_of, provider_cost_of
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation

NOW = datetime(2026, 10, 12, tzinfo=UTC)


def observation(provider="openai", *, grain="turn", **counts):
    return UsageObservation(
        id="observed-work",
        revision=1,
        session=ResourceRef(
            provider=provider,
            kind="session",
            id="owned-session",
            tenant_id="owned-tenant",
            account_scope_id="owned-workspace",
        ),
        model=ModelRef(provider=provider, id="mock-model"),
        grain=grain,
        basis="cumulative",
        observed_at=NOW,
        completeness="partial",
        native_meter={"mock_offline": True},
        **counts,
    )


def price(provider="openai", **overrides):
    return ProviderPrice(
        **dict(
            provider=provider,
            model="mock-model",
            checked_on=NOW.date(),
            input=Decimal(2),
            output=Decimal(10),
            cache_read=Decimal(1),
            cache_write=Decimal(3),
        )
        | overrides
    )


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"input_tokens": 100, "output_tokens": 5}, "0.00015"),
        ({"input_tokens": 100, "input_cached_tokens": 20, "output_tokens": 5}, "0.00023"),
        ({"input_tokens": 100, "input_cache_write_tokens": 10, "output_tokens": 5}, "0.00017"),
        (
            {
                "input_cached_tokens": 20,
                "input_cache_write_tokens": 10,
                "output_reasoning_tokens": 7,
            },
            "0.00012",
        ),
        (
            {
                "input_tokens": 100,
                "input_cached_tokens": 20,
                "input_cache_write_tokens": 10,
                "output_tokens": 5,
            },
            "0.00024",
        ),
        ({"output_tokens": 100, "output_reasoning_tokens": 50}, "0.001"),
    ],
)
def test_independent_valid_categories_survive_unknown_siblings(provider, counts, expected):
    value = observation(provider, **counts)
    original = value.model_dump()
    assert minimum_cost_of(value, price(provider)) == Decimal(expected)
    assert value.model_dump() == original
    # A token floor never manufactures the missing infrastructure or an actual.
    assert provider_cost_of(value, price(provider), infrastructure_usd=None) is None


def test_empty_usage_has_zero_lower_bound_and_unknown_actual():
    value = observation()
    assert minimum_cost_of(value, price()) == 0
    assert provider_cost_of(value, price(), infrastructure_usd=Decimal(0)) is None


def test_full_split_floor_matches_token_cost_without_inventing_runtime():
    value = observation(
        input_tokens=100,
        input_cached_tokens=20,
        input_cache_write_tokens=10,
        output_tokens=5,
    )
    assert minimum_cost_of(value, price()) == provider_cost_of(
        value, price(), infrastructure_usd=Decimal(0)
    )


def test_inapplicable_writes_are_not_an_unknown_discount_stage():
    value = observation(input_tokens=100, input_cached_tokens=0)
    assert minimum_cost_of(value, price(cache_write=None)) == Decimal("0.0002")
    with pytest.raises(ValueError, match="inapplicable"):
        minimum_cost_of(observation(input_cache_write_tokens=1), price(cache_write=None))


def test_inclusive_input_cannot_be_less_than_known_cache_stages():
    with pytest.raises(ValueError, match="inclusive"):
        minimum_cost_of(observation(input_tokens=10, input_cached_tokens=20), price())


@pytest.mark.parametrize("override", [{"verified": False}, {"checked_on": date(2026, 10, 13)}])
def test_unverified_or_future_tariff_cannot_price_a_floor(override):
    assert minimum_cost_of(observation(output_tokens=100), price(**override)) is None
    assert minimum_cost_of(observation(output_tokens=100), None) is None


def test_native_tariff_requires_exact_provider_and_model_identity():
    with pytest.raises(ValueError, match="identity"):
        minimum_cost_of(observation(), price("gemini"))
    with pytest.raises(ValueError, match="identity"):
        minimum_cost_of(observation(), price(model="other-model"))


def test_legacy_prompt_tiers_remain_per_request():
    rates = ModelRates(2, 10, 3, 1, long_context=ModelRates(4, 20, 6, 2))
    assert minimum_cost_of(observation("anthropic", output_tokens=10), rates) == Decimal("0.0001")
    assert minimum_cost_of(
        observation("anthropic", grain="model_request", input_tokens=100_001, output_tokens=10),
        rates,
    ) == Decimal("0.200202")


@pytest.mark.parametrize("grain", ["turn", "session"])
def test_legacy_aggregate_cannot_infer_long_context_request_tier(grain):
    rates = ModelRates(2, 10, 3, 1, long_context=ModelRates(4, 20, 6, 2))
    value = observation(
        "anthropic",
        grain=grain,
        input_tokens=200_000,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=10,
    )
    assert minimum_cost_of(value, rates) == Decimal(".4001")


def test_precision_is_independent_of_callers_decimal_context():
    with localcontext() as context:
        context.prec = 6
        result = minimum_cost_of(
            observation(input_tokens=123456789, input_cached_tokens=1, output_tokens=987654321),
            price(input=Decimal("1.23456789"), output=Decimal("9.87654321")),
        )
    assert result == Decimal("9907.02636516704773")


@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan")])
def test_invalid_legacy_rates_refuse_instead_of_erasing_floor(bad):
    with pytest.raises(ValueError, match="finite nonnegative"):
        minimum_cost_of(observation("anthropic", output_tokens=10), ModelRates(bad, 10, 3, 1))


def test_invalid_reasoning_subset_cannot_inflate_valid_output_floor():
    value = observation(output_tokens=20, output_reasoning_tokens=50)
    assert minimum_cost_of(value, price()) == Decimal(".0002")
    assert value.output_reasoning_tokens == 50  # no meter/DTO repair
