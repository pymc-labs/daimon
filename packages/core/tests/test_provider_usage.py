"""Neutral usage identities, verified spend and real-Postgres settlement proofs."""

import asyncio
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from daimon.core import accounting_outbox
from daimon.core.pricing import ProviderPrice, provider_cost_of, provider_usage_tokens
from daimon.core.stores import mux_state, tenant_ledger, usage_events
from daimon.core.stores.turn_outcomes import list_for_tenant
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.state import UsageTotals
from daimon.core.usage_recording import record_provider_usage
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state.usage_ledger import UsageRevisionConflict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

TENANT = uuid.UUID(int=8)
NOW = datetime(2026, 10, 10, tzinfo=UTC)


def price(provider="openai", **changes):
    return ProviderPrice(
        **{
            "provider": provider,
            "model": "model",
            "checked_on": date(2026, 10, 10),
            "input": Decimal("2"),
            "output": Decimal("10"),
            "cache_read": Decimal("1"),
            **changes,
        }
    )


def observation(provider="openai", revision=1, output=100, **changes):
    return UsageObservation(
        **{
            "id": "usage",
            "revision": revision,
            "session": ResourceRef(
                id="session",
                kind="session",
                provider=provider,
                account_scope_id="workspace",
                tenant_id=str(TENANT),
            ),
            "model": ModelRef(provider=provider, id="model"),
            "grain": "turn",
            "basis": "cumulative",
            "input_tokens": 10,
            "input_cached_tokens": 2,
            "output_tokens": output,
            "native_meter": {"provider_field": {"untouched": True}},
            "completeness": "measured" if output is not None else "partial",
            "observed_at": NOW,
            **changes,
        }
    )


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_cumulative_corrections_replay_and_native_meter(provider):
    first = observation(provider)
    before = first.model_dump(mode="json")
    totals = UsageTotals().add_observation(first)
    assert isinstance(totals, ProviderUsageTotals)
    totals = totals.add_observation(observation(provider, 2, 120))
    totals = totals.add_observation(observation(provider, 3, 110))
    totals = totals.add_observation(first)
    assert totals.output_tokens == totals.reported_output_tokens == 110
    assert totals.input_tokens == 8 and totals.reported_input_tokens == 10
    assert len(totals.observations) == 1
    assert first.model_dump(mode="json") == before
    assert totals.observations[0].input_cache_write_tokens is None


def test_same_revision_conflict_and_unknown_preservation():
    totals = UsageTotals().add_observation(observation(output=None))
    assert totals.reported_output_tokens is None
    with pytest.raises(UsageRevisionConflict):
        totals.add_observation(observation(output=100))
    assert totals.add_observation(observation(revision=2, output=100)).reported_output_tokens == 100


def test_disjoint_interactions_and_explicit_session_coverage():
    totals = UsageTotals().add_observation(observation("gemini", id="one"))
    totals = totals.add_observation(observation("gemini", output=120, id="two"))
    assert totals.output_tokens == 220
    with pytest.raises(ValueError, match="overlapping"):
        totals.add_observation(observation("gemini", id="aggregate", grain="session", output=230))
    totals = totals.add_observation(
        observation("gemini", id="aggregate", grain="session", output=230, covers=("one", "two"))
    )
    assert totals.output_tokens == 230 and len(totals.selected) == 1


def test_legacy_default_serialization_is_exactly_four_stages():
    assert asdict(UsageTotals()) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_exact_decimal_cost_includes_actual_infrastructure(provider):
    usage = observation(provider)
    assert provider_cost_of(
        usage, price(provider), infrastructure_usd=Decimal("0.0053")
    ) == Decimal("0.006318")
    assert provider_usage_tokens(usage, price(provider)).cache_creation_input_tokens == 0
    assert usage.input_cache_write_tokens is None


@pytest.mark.parametrize(
    "change,infra",
    [
        ({"output_tokens": None}, Decimal(0)),
        ({"input_tokens": None}, Decimal(0)),
        ({"input_cached_tokens": None}, Decimal(0)),
        ({}, None),
    ],
)
def test_missing_actual_is_unverified_instead_of_zero(change, infra):
    assert provider_cost_of(observation(**change), price(), infrastructure_usd=infra) is None


def test_equal_input_prices_allow_exact_total_without_fabricating_cached_count():
    usage = observation(input_cached_tokens=None)
    assert provider_cost_of(
        usage, price(cache_read=Decimal(2)), infrastructure_usd=Decimal(0)
    ) == Decimal("0.001020")
    assert usage.input_cached_tokens is None
    assert provider_usage_tokens(usage, price(cache_read=Decimal(2))) is None


def test_estimated_alias_and_invalid_counts_cannot_settle():
    assert (
        provider_cost_of(observation(), price(verified=False), infrastructure_usd=Decimal(0))
        is None
    )
    with pytest.raises(ValueError, match="cached input"):
        provider_cost_of(
            observation(input_cached_tokens=11), price(), infrastructure_usd=Decimal(0)
        )
    with pytest.raises(ValueError, match="inapplicable"):
        provider_cost_of(
            observation(input_cache_write_tokens=1), price(), infrastructure_usd=Decimal(0)
        )


@pytest_asyncio.fixture
async def sessions(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await make_tenant(session, id=TENANT)
    await mux_state.PostgresStateStore(sm).put_binding(
        ProviderBinding(
            id="binding",
            thread=ThreadRef(
                channel=ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel"),
                thread_id="thread",
            ),
            provider="openai",
            profile="openai.persistent_workspace",
            native_refs={"session": "session"},
            generation=1,
            config_revision=1,
        ),
        expected_generation=0,
    )
    return sm


async def record(sm, usage, *, infrastructure=Decimal(0), grain="turn", provider_price=None):
    async with sm() as session, session.begin():
        return await accounting_outbox.record_observation_usage(
            session,
            binding_id="binding",
            observation=usage,
            tenant_id=TENANT,
            platform_user_id="user",
            channel_id="channel",
            pricing=None,
            provider_price=provider_price or price(),
            infrastructure_usd=infrastructure,
            billing_grain=grain,
        )


async def balance(sm):
    async with sm() as session:
        return await tenant_ledger.get_balance(session, tenant_id=TENANT)


async def test_postrun_infrastructure_same_revision_retry_and_replay(sessions):
    usage = observation()
    assert not await record(sessions, usage, infrastructure=None)
    assert await balance(sessions) == 0
    assert len(await mux_state.PostgresStateStore(sessions).pending_outbox(limit=10)) == 1
    assert await record(sessions, usage, infrastructure=Decimal("0.0053"))
    assert await balance(sessions) == Decimal("-0.006318")
    assert not await record(sessions, usage, infrastructure=Decimal("0.0053"))
    assert await balance(sessions) == Decimal("-0.006318")
    async with sessions() as session:
        rows = await usage_events.list_for_tenant(session, tenant_id=TENANT)
    assert len(rows) == 1 and rows[0].input_tokens == 8 and rows[0].cache_creation_input_tokens == 0


async def test_unknown_first_revision_then_verified_correction_no_dropped_input(sessions):
    assert not await record(sessions, observation(output=None))
    assert await record(sessions, observation(revision=2, output=120))
    assert await balance(sessions) == Decimal("-0.001218")
    assert await record(sessions, observation(revision=3, output=110))
    assert await balance(sessions) == Decimal("-0.001118")
    assert await mux_state.PostgresStateStore(sessions).pending_outbox(limit=10)


async def test_signed_correction_and_actual_infrastructure_revision(sessions):
    assert await record(sessions, observation(), infrastructure=Decimal("0.005"))
    assert await record(
        sessions, observation(revision=2, output=120), infrastructure=Decimal("0.006")
    )
    assert await record(
        sessions, observation(revision=3, output=110), infrastructure=Decimal("0.0055")
    )
    assert await balance(sessions) == Decimal("-0.006618")
    async with sessions() as session:
        assert await tenant_ledger.get_channel_spend(
            session,
            tenant_id=TENANT,
            channel_id="channel",
            since=NOW,
            until=NOW + timedelta(days=1),
        ) == Decimal("0.006618")


async def test_turn_and_session_cannot_both_charge(sessions):
    assert await record(sessions, observation())
    with pytest.raises(ValueError, match="one billing grain"):
        await record(sessions, observation(id="aggregate", grain="session"), grain="session")
    assert await balance(sessions) == Decimal("-0.001018")


async def test_out_of_order_verified_rows_cannot_rewind_the_absolute_charge(sessions):
    store = mux_state.PostgresStateStore(sessions)
    first = await store.record_usage("binding", observation())
    second = await store.record_usage("binding", observation(revision=2, output=120))
    for row in (second, first):
        async with sessions() as session, session.begin():
            assert await accounting_outbox.apply_usage_outbox(
                session,
                row,
                tenant_id=TENANT,
                platform_user_id="user",
                channel_id="channel",
                pricing=None,
                provider_price=price(),
                infrastructure_usd=Decimal(0),
                billing_grain="turn",
            )
    assert await balance(sessions) == Decimal("-0.001218")


async def test_rollback_leaves_pending_and_restored_recorder_settles_once(sessions, monkeypatch):
    original = tenant_ledger.insert_entry

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated crash")

    with monkeypatch.context() as patch:
        patch.setattr(tenant_ledger, "insert_entry", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await record(sessions, observation())
    assert await balance(sessions) == 0
    assert await record(sessions, observation())
    assert await balance(sessions) == Decimal("-0.001018")


async def test_cross_tenant_observation_is_refused_before_charge(sessions):
    usage = observation(
        session=observation().session.model_copy(update={"tenant_id": str(uuid.UUID(int=9))})
    )
    with pytest.raises(ScopeViolation):
        await record(sessions, usage)
    assert await balance(sessions) == 0


def test_a_coverage_cycle_cannot_hide_beside_an_unrelated_observation():
    totals = UsageTotals().add_observation(observation(id="unrelated"))
    totals = totals.add_observation(observation(id="one", covers=("two",)))
    with pytest.raises(ValueError, match="cyclic"):
        totals.add_observation(observation(id="two", covers=("one",)))


async def test_concurrent_duplicate_actual_and_different_grains(sessions):
    results = await asyncio.gather(*(record(sessions, observation()) for _ in range(2)))
    assert sorted(results) == [False, True]
    assert await balance(sessions) == Decimal("-0.001018")


async def test_concurrent_first_turn_and_session_cannot_both_settle(sessions):
    results = await asyncio.gather(
        record(sessions, observation()),
        record(sessions, observation(id="aggregate", grain="session"), grain="session"),
        return_exceptions=True,
    )
    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert await balance(sessions) == Decimal("-0.001018")


async def test_covered_aggregate_waits_for_latest_leaf_actual(sessions):
    assert await record(sessions, observation())
    assert not await record(sessions, observation(revision=2, output=120), infrastructure=None)
    aggregate = observation(id="aggregate", grain="session", output=120, covers=("usage",))
    assert not await record(sessions, aggregate)
    assert await record(sessions, observation(revision=2, output=120))
    assert await record(sessions, aggregate)
    assert await balance(sessions) == Decimal("-0.001218")


async def test_root_model_binding_and_unknown_child_outcomes(sessions):
    outcome = TurnObservation(sessions, TENANT, "slack", session_id="session")

    async def capture(usage):
        return await record_provider_usage(
            sessionmaker=sessions,
            binding_id="binding",
            observation=usage,
            tenant_id=TENANT,
            platform_user_id="user",
            provider_price=price(),
            infrastructure_usd=Decimal("0.0053"),
            billing_grain="turn",
            channel_id="channel",
        )

    with outcome.activate():
        assert await capture(observation(model=None))
        assert not await capture(observation(id="child", model=None, thread_id="child-thread"))
        outcome.finish()
    await drain_outcomes()
    async with sessions() as session:
        rows = await list_for_tenant(session, TENANT)
    assert len(rows) == 1 and rows[0].model_calls == 2
    assert rows[0].cost_usd is None and rows[0].unpriced_calls == 1
    assert rows[0].billing_posture == "metered"
    assert await balance(sessions) == Decimal("-0.006318")


async def test_outcomes_replace_revisions_include_actual_and_keep_unknown(sessions):
    outcome = TurnObservation(sessions, TENANT, "slack", session_id="session")
    outcome.note_usage(
        observation(), metered=True, provider_price=price(), infrastructure_usd=Decimal("0.0053")
    )
    outcome.note_usage(
        observation(revision=2, output=120),
        metered=True,
        provider_price=price(),
        infrastructure_usd=Decimal("0.006"),
    )
    outcome.note_usage(observation())  # stale replay does not rewind known actual
    outcome.finish()
    await drain_outcomes()
    async with sessions() as session:
        rows = await list_for_tenant(session, TENANT)
    assert rows[0].model_calls == 1 and rows[0].output_tokens == 120
    assert rows[0].input_tokens == 8 and rows[0].cache_creation_input_tokens == 0
    assert rows[0].cost_usd == Decimal("0.007218") and rows[0].unpriced_calls == 0
    assert rows[0].usage_refs == [{"session_id": "session", "event_id": "usage"}]


async def test_unknown_later_bucket_does_not_bill_carried_forward_counts(sessions):
    assert await record(sessions, observation())
    assert not await record(sessions, observation(revision=2, input_tokens=None, output=120))
    assert await balance(sessions) == Decimal("-0.001018")
    assert await record(sessions, observation(revision=3, output=120))
    assert await balance(sessions) == Decimal("-0.001218")


async def test_same_revision_changed_native_attribution_is_refused(sessions):
    assert not await record(sessions, observation(), infrastructure=None)
    with pytest.raises(UsageRevisionConflict):
        await record(sessions, observation(thread_id="different-child"))
    assert await balance(sessions) == 0


async def test_zero_first_verified_correction_still_pins_billing_context(sessions):
    assert not await record(sessions, observation(output=None))
    free = price(input=Decimal(0), output=Decimal(0), cache_read=Decimal(0))
    assert await record(sessions, observation(revision=2), provider_price=free)
    with pytest.raises(ScopeViolation):
        async with sessions() as session, session.begin():
            await accounting_outbox.record_observation_usage(
                session,
                binding_id="binding",
                observation=observation(revision=3),
                tenant_id=TENANT,
                platform_user_id="user",
                channel_id="wrong-channel",
                pricing=None,
                provider_price=price(),
                infrastructure_usd=Decimal(0),
                billing_grain="turn",
            )
    assert await balance(sessions) == 0


async def test_external_producer_transaction_and_host_recorder_share_lock_order(
    sessions, db_nullpool_engine
):
    captured, apply = asyncio.Event(), asyncio.Event()
    competing_pid = asyncio.get_running_loop().create_future()

    async def external_composition():
        async with sessions() as session, session.begin():
            row = await mux_state.record_usage(session, "binding", observation())
            assert row is not None
            captured.set()
            await apply.wait()
            return await accounting_outbox.apply_usage_outbox(
                session,
                row,
                tenant_id=TENANT,
                platform_user_id="user",
                pricing=None,
                provider_price=price(),
                infrastructure_usd=Decimal(0),
                channel_id="channel",
                billing_grain="turn",
            )

    async def host_recorder():
        await captured.wait()
        async with sessions() as session, session.begin():
            competing_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            return await accounting_outbox.record_observation_usage(
                session,
                binding_id="binding",
                observation=observation(),
                tenant_id=TENANT,
                platform_user_id="user",
                pricing=None,
                provider_price=price(),
                infrastructure_usd=Decimal(0),
                channel_id="channel",
                billing_grain="turn",
            )

    external = asyncio.create_task(external_composition())
    host = asyncio.create_task(host_recorder())
    pid = await competing_pid
    probe_sessions = async_sessionmaker(db_nullpool_engine)

    async def blocked():
        while True:
            async with probe_sessions() as session:
                event = await session.scalar(
                    text("SELECT wait_event FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
                )
            if event == "advisory":
                return
            await asyncio.sleep(0.01)

    # Both implementations wait on the producer here. With inverted ordering
    # the competitor also holds the binding lock: external apply deadlocks.
    try:
        await asyncio.wait_for(blocked(), 2)
        apply.set()
        assert await asyncio.wait_for(asyncio.gather(external, host), 5) == [True, False]
    finally:
        for task in (external, host):
            if not task.done():
                task.cancel()
        await asyncio.gather(external, host, return_exceptions=True)
    assert await balance(sessions) == Decimal("-0.001018")
