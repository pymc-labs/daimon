"""Web searches: list-priced past the provider's free allowance, counted once."""

import asyncio
import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from daimon.core import accounting_outbox
from daimon.core.pricing import (
    ProviderPrice,
    WebSearchPrice,
    provider_cost_of,
    provider_price_on,
    provider_prices,
    web_search_cost,
)
from daimon.core.stores import mux_state, tenant_ledger, usage_events, web_search_usage
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.profiles.gemini import INLINE_REUSE
from mux.profiles.openai import PERSISTENT_WORKSPACE
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

TENANT = uuid.UUID("a1000000-0000-0000-0000-000000000001")
TENANT2 = uuid.UUID("b2000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 10, 10, tzinfo=UTC)
TOKENS = Decimal("0.001018")  # the observation's token cost at price()
GEMINI = WebSearchPrice(Decimal("0.014"), free_monthly=5, pool="gemini-3")
# binding id -> (provider, native session, tenant)
BINDINGS = {
    "openai": ("openai", "session-o", TENANT),
    "gemini-a": ("gemini", "session-a", TENANT),
    "gemini-b": ("gemini", "session-b", TENANT2),
}


def price(provider="gemini", web_search=GEMINI, **changes):
    return ProviderPrice(
        **{
            "provider": provider,
            "model": "model",
            "checked_on": date(2026, 10, 10),
            "input": Decimal("2"),
            "output": Decimal("10"),
            "cache_read": Decimal("1"),
            "web_search": web_search,
            **changes,
        }
    )


def observation(binding="gemini-a", searches=0, revision=1, model="model", **changes):
    provider, session, tenant = BINDINGS[binding]
    return UsageObservation(
        **{
            "id": f"usage-{binding}",
            "revision": revision,
            "session": ResourceRef(
                id=session,
                kind="session",
                provider=provider,
                account_scope_id="project",
                tenant_id=str(tenant),
            ),
            "model": ModelRef(provider=provider, id=model),
            "grain": "turn",
            "basis": "cumulative",
            "input_tokens": 10,
            "input_cached_tokens": 2,
            "output_tokens": 100,
            "web_searches": searches,
            "completeness": "measured",
            "observed_at": NOW,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ((Decimal("-0.01"),), "finite nonnegative"),
        ((Decimal("NaN"),), "finite nonnegative"),
        ((Decimal("0.01"), -1, "pool"), "nonnegative integer"),
        ((Decimal("0.01"), 5), "needs its pool"),
    ],
)
def test_web_search_price_refuses_bad_rates(args, match):
    with pytest.raises(ValueError, match=match):
        WebSearchPrice(*args)


def test_every_current_tariff_prices_its_searches():
    """A tariff without a search rate would leave every searching turn pending."""
    for profile, rate in (
        (PERSISTENT_WORKSPACE.profile_id, WebSearchPrice(Decimal("0.01"))),
        (INLINE_REUSE.profile_id, WebSearchPrice(Decimal("0.014"), 5_000, "gemini-3")),
    ):
        prices = provider_prices(profile)
        assert prices and {p.web_search for p in prices.values()} == {rate}, profile


def test_search_cost_bills_only_paid_searches():
    usage, rate = observation(searches=3), price()
    assert provider_cost_of(usage, rate, infrastructure_usd=Decimal(0)) == TOKENS + Decimal(
        "0.042"
    ), "no paid count is the all-paid upper bound"
    assert provider_cost_of(
        usage, rate, infrastructure_usd=Decimal(0), paid_web_searches=1
    ) == TOKENS + Decimal("0.014")
    assert web_search_cost(usage, rate, 0) == 0
    with pytest.raises(ValueError, match="exceed"):
        web_search_cost(usage, rate, 4)
    unpriced = price(web_search=None)
    assert web_search_cost(usage, unpriced) is None
    assert provider_cost_of(usage, unpriced, infrastructure_usd=Decimal(0)) is None
    assert provider_cost_of(observation(), unpriced, infrastructure_usd=Decimal(0)) == TOKENS


def test_allowance_month_follows_pacific_time():
    edge = datetime(2026, 12, 1, 8, tzinfo=UTC)
    assert web_search_usage.period_of(edge - timedelta(minutes=1)) == date(2026, 11, 1)
    assert web_search_usage.period_of(edge) == date(2026, 12, 1)


@pytest_asyncio.fixture
async def sessions(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await make_tenant(session, id=TENANT)
        await make_tenant(session, id=TENANT2)
    store = mux_state.PostgresStateStore(sm)
    for binding, (provider, native, tenant) in BINDINGS.items():
        await store.put_binding(
            ProviderBinding(
                id=binding,
                thread=ThreadRef(
                    channel=ChannelRef(tenant_id=str(tenant), platform="slack", channel_id="c"),
                    thread_id=binding,
                ),
                provider=provider,
                profile=(PERSISTENT_WORKSPACE if provider == "openai" else INLINE_REUSE).profile_id,
                native_refs={"session": native},
                generation=1,
                config_revision=1,
            ),
            expected_generation=0,
        )
    return sm


async def settle(session, usage, provider_price=None):
    binding = usage.id.removeprefix("usage-")
    return await accounting_outbox.record_observation_usage(
        session,
        binding_id=binding,
        observation=usage,
        tenant_id=BINDINGS[binding][2],
        platform_user_id="user",
        channel_id="c",
        pricing=None,
        provider_price=provider_price or price(BINDINGS[binding][0]),
        infrastructure_usd=Decimal(0),
        billing_grain="turn",
    )


async def record(sm, usage, provider_price=None):
    async with sm() as session, session.begin():
        return await settle(session, usage, provider_price)


async def balance(sm, tenant=TENANT):
    async with sm() as session:
        return await tenant_ledger.get_balance(session, tenant_id=tenant)


async def used(sm):
    async with sm() as session:
        return await session.scalar(
            text("SELECT coalesce(sum(used), 0) FROM web_search_allowances")
        )


async def test_openai_bills_every_search_once_across_replays(sessions):
    rate = price("openai", WebSearchPrice(Decimal("0.01")))
    assert await record(sessions, observation("openai", 2), rate)
    assert not await record(sessions, observation("openai", 2), rate)
    assert await balance(sessions) == -(TOKENS + Decimal("0.02"))
    assert await record(sessions, observation("openai", 5, revision=2), rate)
    assert await balance(sessions) == -(TOKENS + Decimal("0.05")), "a revision bills its delta"
    assert await used(sessions) == 0, "a tariff with no allowance claims none"


async def test_allowance_is_shared_across_tenants_and_bills_past_it(sessions):
    assert await record(sessions, observation("gemini-a", 3))
    assert await balance(sessions) == -TOKENS
    assert await record(sessions, observation("gemini-b", 4))
    assert await balance(sessions, TENANT2) == -(TOKENS + 2 * Decimal("0.014"))
    assert await record(sessions, observation("gemini-a", 6, revision=2))
    assert await balance(sessions) == -(TOKENS + 3 * Decimal("0.014")), (
        "a later revision's extra searches claim the allowance another tenant used up"
    )
    assert await used(sessions) == 10


async def test_replays_and_lower_counts_never_claim_twice(sessions):
    assert await record(sessions, observation(searches=6))
    assert not await record(sessions, observation(searches=6))
    assert await balance(sessions) == -(TOKENS + Decimal("0.014"))
    assert await record(sessions, observation(searches=4, revision=2))
    assert await balance(sessions) == -TOKENS, "a lower count keeps the free searches it had"
    assert await record(sessions, observation(searches=6, revision=3))
    assert await balance(sessions) == -(TOKENS + Decimal("0.014"))
    assert await used(sessions) == 6
    async with sessions() as session:
        row = (
            await session.execute(
                text("SELECT web_searches, free_web_searches, paid_web_searches FROM usage_events")
            )
        ).one()
    assert tuple(row) == (6, 5, 1)


async def test_stale_revision_applied_after_a_newer_one_claims_nothing(sessions):
    store = mux_state.PostgresStateStore(sessions)
    older = await store.record_usage("gemini-a", observation(searches=7))
    newer = await store.record_usage("gemini-a", observation(searches=2, revision=2))
    for row in (newer, older):
        async with sessions() as session, session.begin():
            assert await accounting_outbox.apply_usage_outbox(
                session,
                row,
                tenant_id=TENANT,
                platform_user_id="user",
                channel_id="c",
                pricing=None,
                provider_price=price(),
                infrastructure_usd=Decimal(0),
                billing_grain="turn",
            )
    assert await used(sessions) == 2 and await balance(sessions) == -TOKENS


async def test_crossing_turn_waits_for_the_claim_it_follows(sessions):
    """Two turns crossing the limit: the second counts on top of the first's commit."""
    async with sessions() as session, session.begin():
        assert await settle(session, observation("gemini-a", 4))
        crossing = asyncio.create_task(record(sessions, observation("gemini-b", 2)))
        await asyncio.sleep(0.3)
        assert not crossing.done(), "the crossing claim must wait on the allowance row"
    assert await crossing
    assert await balance(sessions) == -TOKENS
    assert await balance(sessions, TENANT2) == -(TOKENS + Decimal("0.014"))


async def test_concurrent_turns_split_the_allowance_exactly(sessions):
    assert all(
        await asyncio.gather(
            record(sessions, observation("gemini-a", 4)),
            record(sessions, observation("gemini-b", 4)),
        )
    )
    total = await balance(sessions) + await balance(sessions, TENANT2)
    assert total == -(2 * TOKENS + 3 * Decimal("0.014"))


async def test_spend_reads_match_the_ledger(sessions):
    at = datetime(2026, 11, 15, tzinfo=UTC)
    real = provider_price_on("gemini-3.8-flash", at.date())
    assert real is not None and real.web_search is not None
    rate = dataclasses.replace(
        real, web_search=dataclasses.replace(real.web_search, free_monthly=2)
    )
    usage = observation(searches=5, model="gemini-3.8-flash", observed_at=at)
    assert await record(sessions, usage, rate)
    async with sessions() as session:
        spend = await usage_events.cost_for_tenant_since(
            session, tenant_id=TENANT, since=at - timedelta(days=1)
        )
    # The ledger rounds each debit to the micro-dollar; the read reprices exactly.
    assert spend == pytest.approx(float(-await balance(sessions)), abs=5e-7)
    assert -await balance(sessions) > 3 * real.web_search.usd
