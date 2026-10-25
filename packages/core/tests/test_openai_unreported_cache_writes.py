"""ROOT D2=B: an OpenAI turn whose meter has no cache-write count is priced as writing
none, never above what its counts prove, and its debit says it is an estimate.

OpenAI bills cache writes on GPT-5.6 and later at 1.25x uncached input (prompt-caching
guide; the luna page lists "Cache writes $0.125 / 1M"), but the Agents API turn usage
"do[es] not expose a separate cache-write count" (Agents API observability). Before,
`provider_cost_of` refused every such row (the tariff has a cache-write rate, the meter
no count), so no OpenAI turn was ever debited. The rows below are staging's.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core._models import TenantLedger
from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.pricing import provider_cost_of, provider_prices, unreported_cache_writes
from daimon.core.stores import mux_state
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.profiles.openai import CONVERSATION_ONLY
from sqlalchemy import select

from .test_fleet_budget import setup
from .test_fleet_budget import sm as sm

PRICES = provider_prices(CONVERSATION_ONLY.profile_id)


def turn(model: str, inputs, cached, outputs, **changes) -> UsageObservation:
    """`mux.drivers.openai.usage.observation`'s shape: no cache-write count."""
    fields: dict[str, object] = dict(
        id="openai:turn:turn_1",
        revision=1,
        session=ResourceRef(
            provider="openai",
            kind="session",
            id="conv_1",
            tenant_id="tenant",
            account_scope_id="daimon-deployment",
        ),
        turn_id="turn_1",
        model=ModelRef(provider="openai", id=model),
        grain="turn",
        basis="cumulative",
        input_tokens=inputs,
        input_cached_tokens=cached,
        output_tokens=outputs,
        native_meter={},
        completeness="measured",
        observed_at=datetime.now(UTC),
    )
    return UsageObservation.model_validate(fields | changes)


@pytest.mark.parametrize(
    ("model", "inputs", "outputs", "usd"),
    [
        # Staging's luna row: 13,351 in at $0.10 + 61 out at $0.50, per million.
        ("gpt-6-luna", 13_351, 61, Decimal("0.0013656")),
        # Staging's astra row: 13,969 in at $10 + 18 out at $50, per million.
        ("gpt-6-astra", 13_969, 18, Decimal("0.14059")),
    ],
)
def test_a_real_turn_without_a_cache_write_count_is_priced_as_writing_none(
    model, inputs, outputs, usd
):
    price = PRICES[("openai", model)]
    usage = turn(model, inputs, 0, outputs)
    assert price.cache_write is not None and usage.input_cache_write_tokens is None
    assert unreported_cache_writes(usage, price)
    assert provider_cost_of(usage, price, infrastructure_usd=Decimal(0)) == usd


def test_cached_input_is_read_at_its_rate_and_the_rest_at_the_uncached_rate():
    price = PRICES[("openai", "gpt-6-luna")]
    # 9,351 uncached at $0.10 + 4,000 cached at $0.01 + 61 out at $0.50, per million.
    assert provider_cost_of(
        turn("gpt-6-luna", 13_351, 4_000, 61), price, infrastructure_usd=Decimal(0)
    ) == Decimal("0.0010056")


def test_a_reported_cache_write_count_is_billed_at_the_write_rate_not_estimated():
    price = PRICES[("openai", "gpt-6-luna")]
    usage = turn("gpt-6-luna", 13_351, 0, 61, input_cache_write_tokens=10_000)
    assert not unreported_cache_writes(usage, price)
    # 3,351 uncached at $0.10 + 10,000 written at $0.125 + 61 out at $0.50.
    assert provider_cost_of(usage, price, infrastructure_usd=Decimal(0)) == Decimal("0.0016156")


def test_unknown_counts_stay_unpriced():
    price = PRICES[("openai", "gpt-6-luna")]
    for usage in (
        turn("gpt-6-luna", 13_351, None, 61, completeness="partial"),
        turn("gpt-6-luna", None, None, None, completeness="unknown"),
    ):
        assert provider_cost_of(usage, price, infrastructure_usd=Decimal(0)) is None


def test_above_the_long_context_threshold_astra_keeps_its_long_context_rates():
    price = PRICES[("openai", "gpt-6-astra")]
    # 272,001 uncached at $20 + 10k out at $75 per million (the whole aggregate, 2x/1.5x).
    assert provider_cost_of(
        turn("gpt-6-astra", 272_001, 0, 10_000), price, infrastructure_usd=Decimal(0)
    ) == Decimal("6.19002")


def test_a_provider_without_a_cache_write_rate_is_never_an_estimate():
    gemini = provider_prices("gemini.inline_reuse")
    price = next(iter(gemini.values()))
    usage = turn("x", 100, 0, 10).model_copy(
        update={
            "session": turn("x", 100, 0, 10).session.model_copy(update={"provider": "gemini"}),
            "model": ModelRef(provider="gemini", id=price.model),
        }
    )
    assert not unreported_cache_writes(usage, price)


async def _bind(sm, run, parent, profile, provider):
    binding = ProviderBinding(
        id=f"binding-{provider}",
        thread=parent.fleet.root.model_copy(update={"thread_id": f"thread-{provider}"}),
        legacy_account_id=str(parent.fleet.account_id),
        provider=provider,
        profile=profile,
        native_refs={"session": f"native-{provider}"},
        generation=1,
        config_revision=0,
    )
    async with sm() as db, db.begin():
        await mux_state.put_binding(db, binding, expected_generation=0)
    return binding


async def test_the_estimate_is_marked_on_its_debit_and_absent_on_a_metered_one(sm):
    parent, run = await setup(sm, funds="10", cap="2")
    native = ResourceRef(
        provider="openai",
        kind="session",
        id="native-openai",
        tenant_id=str(run.tenant_id),
        account_scope_id="daimon-deployment",
    )
    openai = await _bind(sm, run, parent, CONVERSATION_ONLY.profile_id, "openai")
    estimated = turn("gpt-6-luna", 13_351, 0, 61).model_copy(update={"session": native})
    gemini_price = provider_prices("gemini.inline_reuse")[("gemini", "gemini-3.8-flash")]
    gemini = await _bind(sm, run, parent, "gemini.inline_reuse", "gemini")
    metered = UsageObservation(
        id="gemini:interaction:usage",
        revision=1,
        session=native.model_copy(update={"provider": "gemini", "id": "native-gemini"}),
        turn_id="interaction",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        grain="turn",
        basis="cumulative",
        input_tokens=4_000,
        input_cached_tokens=1_000,
        output_tokens=300,
        native_meter={},
        completeness="measured",
        observed_at=datetime.now(UTC),
    )
    for binding, usage, price in (
        (openai, estimated, PRICES[("openai", "gpt-6-luna")]),
        (gemini, metered, gemini_price),
    ):
        async with sm() as db, db.begin():
            assert await record_observation_usage(
                db,
                binding_id=binding.id,
                observation=usage,
                tenant_id=run.tenant_id,
                platform_user_id="tester",
                pricing=None,
                provider_price=price,
                infrastructure_usd=Decimal(0),
                billing_grain="turn",
                channel_id=run.channel_id,
            )
    async with sm() as db:
        marks = dict(
            (
                await db.execute(
                    select(TenantLedger.idempotency_key, TenantLedger.estimate).where(
                        TenantLedger.tenant_id == run.tenant_id, TenantLedger.delta_usd < 0
                    )
                )
            ).all()
        )
    assert marks == {
        "turn:native-openai:openai:turn:turn_1": "cache_writes_unreported",
        "turn:native-gemini:gemini:interaction:usage": None,
    }
