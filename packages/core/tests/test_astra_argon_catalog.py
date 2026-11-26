"""gpt-6-astra, the GPT-6.2 models and the Gemini opt-in models: priced agent model picks."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from daimon.core.pricing import PROVIDER_PRICING_SOURCES, provider_cost_of, provider_prices
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from mux.profiles.gemini import INLINE_REUSE, OPT_IN_MODELS
from mux.profiles.openai import NATIVE_MODELS, PERSISTENT_WORKSPACE

ASTRA = "gpt-6-astra"
CHANNEL = ChannelRef(tenant_id="t", platform="discord", channel_id="c")


def astra(profile: str = PERSISTENT_WORKSPACE.profile_id) -> ConfigRevision:
    return ConfigRevision.create(
        CHANNEL, 1, resolve_default(BackendConfig(backend="openai", profile=profile, model=ASTRA))
    )


def test_astra_has_a_dated_verified_price_row() -> None:
    price = provider_prices(PERSISTENT_WORKSPACE.profile_id)[("openai", ASTRA)]
    assert (price.input, price.output, price.cache_read, price.cache_write) == (
        Decimal("10.00"),
        Decimal("50.00"),
        Decimal("1.00"),
        Decimal("12.50"),
    )
    assert price.max_priced_input_tokens == 272_000
    assert "https://developers.openai.com/api/docs/models/gpt-6-astra" in PROVIDER_PRICING_SOURCES


@pytest.mark.parametrize(
    ("model", "rates"),
    [
        ("gpt-6.2-astra", ("8.00", "40.00", "0.40", "10.00")),
        ("gpt-6.2-sol", ("2.00", "10.00", "0.10", "2.50")),
    ],
)
def test_gpt_6_2_models_have_dated_sourced_rows_with_astras_long_context_rule(
    model: str, rates: tuple[str, ...]
) -> None:
    """Each row is its model page's rates from 2026-11-17, with Astra's rule above 272k
    input tokens and web search price, and the native host runs the model."""
    rows = provider_prices(PERSISTENT_WORKSPACE.profile_id)
    price, astra_row = rows[("openai", model)], rows[("openai", ASTRA)]
    assert (price.input, price.output, price.cache_read, price.cache_write) == tuple(
        map(Decimal, rates)
    ), f"{model}: input, output, cached input, cache write"
    assert price.checked_on == date(2026, 11, 17), "dated from the day it was checked"
    assert (price.max_priced_input_tokens, price.long_context_multipliers) == (
        astra_row.max_priced_input_tokens,
        astra_row.long_context_multipliers,
    ), "priced wholly at 2x input and cache, 1.5x output above 272k, as Astra"
    assert price.web_search == astra_row.web_search, "web search at OpenAI's per-call rate"
    assert f"https://developers.openai.com/api/docs/models/{model}" in PROVIDER_PRICING_SOURCES, (
        "the model page is a cited source"
    )
    assert model in NATIVE_MODELS, "the native OpenAI host runs it"


@pytest.mark.parametrize("model", OPT_IN_MODELS)
def test_gemini_opt_in_models_have_a_flat_sourced_standard_price(model: str) -> None:
    price = provider_prices(INLINE_REUSE.profile_id)[("gemini", model)]
    assert price.max_priced_input_tokens is None, "no long-context tier"
    assert price.long_context_multipliers is None
    assert price.cache_write is None, "implicit caching has no write rate"
    assert "https://aistudio.google.com/docs/pricing" in PROVIDER_PRICING_SOURCES


def usage(**changes: object) -> UsageObservation:
    fields: dict[str, object] = dict(
        id="usage",
        revision=1,
        session=ResourceRef(
            provider="openai",
            kind="session",
            id="session",
            tenant_id="tenant",
            account_scope_id="account",
        ),
        model=ModelRef(provider="openai", id=ASTRA),
        grain="session",
        basis="cumulative",
        observed_at=datetime(2026, 10, 22, tzinfo=UTC),
        completeness="measured",
        native_meter={},
        input_tokens=100_000,
        input_cached_tokens=20_000,
        input_cache_write_tokens=0,
        output_tokens=10_000,
    )
    return UsageObservation.model_validate(fields | changes)


def test_astra_usage_is_priced_at_its_rate_and_per_request_above_272k() -> None:
    price = provider_prices(PERSISTENT_WORKSPACE.profile_id)[("openai", ASTRA)]
    # 80k uncached at $10 + 20k cached at $1 + 10k output at $50, per million.
    assert provider_cost_of(usage(), price, infrastructure_usd=Decimal(0)) == Decimal("1.32")
    # Above 272k input OpenAI doubles input/cache and charges 1.5x output, per request:
    # 272,001 uncached at $20 + 10k output at $75, per million.
    assert provider_cost_of(
        usage(input_tokens=272_001, input_cached_tokens=0, grain="model_request"),
        price,
        infrastructure_usd=Decimal(0),
    ) == Decimal("6.19002")
    # A session total may sum many short requests, so it keeps the base rate.
    assert provider_cost_of(
        usage(input_tokens=272_001, input_cached_tokens=0),
        price,
        infrastructure_usd=Decimal(0),
    ) == Decimal("3.22001")
    # At exactly the threshold, the base rate.
    assert provider_cost_of(
        usage(input_tokens=272_000, input_cached_tokens=0),
        price,
        infrastructure_usd=Decimal(0),
    ) == Decimal("3.22")


def test_long_context_multipliers_need_a_threshold() -> None:
    with pytest.raises(ValueError, match="long-context"):
        replace(
            provider_prices(PERSISTENT_WORKSPACE.profile_id)[("openai", ASTRA)],
            max_priced_input_tokens=None,
        )
