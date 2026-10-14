"""Native child identities and root-attributed atomic Postgres money proofs."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from daimon.core.pricing import ProviderPrice
from daimon.core.stores import mux_state, tenant_ledger
from daimon.core.stores.turn_outcomes import list_for_tenant
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_delegation import (
    RootUsageMeasurement,
    RootUsageMember,
    RootUsageScope,
    prepare_root_usage,
    record_root_usage,
)
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ScopeViolation
from mux.state.usage_ledger import UsageRevisionConflict
from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

TENANT = uuid.UUID(int=808)
NOW = datetime(2026, 10, 10, tzinfo=UTC)
SESSION = ResourceRef(
    id="child-usage-session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id=str(TENANT),
    account_id="account",
)
ROOT_MODEL = ModelRef(provider="openai", id="root-model")
CHILD_MODEL = ModelRef(provider="openai", id="child-model")


def root_scope(
    *,
    child_model=CHILD_MODEL,
    root="root",
    child="child",
    parent=None,
    disjoint=True,
    inventory_complete=True,
):
    return RootUsageScope(
        SESSION,
        root,
        (
            RootUsageMember(root, None, None, ROOT_MODEL),
            RootUsageMember(child, parent or root, "subagent", child_model),
        ),
        disjoint=disjoint,
        inventory_complete=inventory_complete,
    )


def tariff(model, inputs="2", outputs="10"):
    return ProviderPrice(
        "openai",
        model.id,
        date(2026, 10, 10),
        Decimal(inputs),
        Decimal(outputs),
        Decimal("1"),
    )


def native_usage(turn="root", *, revision=1, inputs=100, outputs=10, **changes):
    return UsageObservation(
        **{
            "id": f"openai:turn:{turn}",
            "revision": revision,
            "session": SESSION,
            "turn_id": turn,
            "thread_id": None if turn == "root" else "subagent",
            "grain": "turn",
            "basis": "cumulative",
            "input_tokens": inputs,
            "input_cached_tokens": 0,
            "output_tokens": outputs,
            "completeness": "unknown" if inputs is None else "measured",
            "observed_at": NOW,
            "native_meter": {}
            if inputs is None
            else {"input_tokens": inputs, "output_tokens": outputs},
            **changes,
        }
    )


def measurements(*, child=None, root=None, child_price=None):
    return (
        RootUsageMeasurement(root or native_usage(), tariff(ROOT_MODEL)),
        RootUsageMeasurement(
            child or native_usage("child", inputs=1_000_000, outputs=2),
            child_price or tariff(CHILD_MODEL, "3", "20"),
        ),
    )


@pytest_asyncio.fixture
async def sessions(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await make_tenant(session, id=TENANT)
    await mux_state.PostgresStateStore(sm).put_binding(
        ProviderBinding(
            id="child-binding",
            thread=ThreadRef(
                channel=ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel"),
                thread_id="thread",
            ),
            provider="openai",
            profile="openai.persistent_workspace",
            native_refs={"session": SESSION.id},
            generation=1,
            config_revision=1,
        ),
        expected_generation=0,
    )
    return sm


async def record(sm, values=None, *, scope=None, infrastructure=Decimal(".03")):
    return await record_root_usage(
        sessionmaker=sm,
        binding_id="child-binding",
        scope=scope or root_scope(),
        measurements=values or measurements(),
        tenant_id=TENANT,
        platform_user_id="user",
        infrastructure_usd=infrastructure,
        channel_id="channel",
    )


async def balance(sm):
    async with sm() as session:
        return await tenant_ledger.get_balance(session, tenant_id=TENANT)


def test_projection_preserves_native_ids_threads_and_meters_and_disjoint_totals():
    values = measurements()
    before = [m.observation.model_dump(mode="json") for m in values]
    prepared = prepare_root_usage(root_scope(), values)
    assert {m.observation.turn_id for m in prepared} == {"root"}
    totals = ProviderUsageTotals()
    for measurement in prepared:
        totals = totals.add_observation(measurement.observation)
        original = next(
            m.observation for m in values if m.observation.id == measurement.observation.id
        )
        assert measurement.observation.native_meter == original.native_meter
        assert measurement.observation.thread_id == original.thread_id
    assert totals.reported_input_tokens == 1_000_100 and totals.output_tokens == 12
    assert len(totals.selected) == 2
    assert [m.observation.model_dump(mode="json") for m in values] == before


@pytest.mark.parametrize(
    "defect",
    [
        "old_parent",
        "cycle",
        "absent_parent",
        "duplicate",
        "root_child",
        "no_thread",
        "foreign_model",
    ],
)
def test_ancestry_requires_native_links_to_this_root(defect):
    scope = root_scope()
    root, child = scope.members
    if defect == "old_parent":
        child = replace(child, parent_turn_id="old-root")
    elif defect == "cycle":
        child = replace(child, parent_turn_id="child")
    elif defect == "absent_parent":
        child = replace(child, parent_turn_id=None)
    elif defect == "duplicate":
        child = root
    elif defect == "root_child":
        root = replace(root, thread_id="subagent")
    elif defect == "no_thread":
        child = replace(child, thread_id=None)
    else:
        child = replace(child, model=ModelRef(provider="gemini", id="model"))
    with pytest.raises(ValueError):
        RootUsageScope(SESSION, "root", (root, child))


def test_nested_children_with_reused_subagent_have_distinct_native_work():
    scope = root_scope()
    scope = replace(
        scope,
        members=(*scope.members, RootUsageMember("grandchild", "child", "subagent", CHILD_MODEL)),
    )
    values = (
        *measurements(),
        RootUsageMeasurement(native_usage("grandchild", inputs=20), tariff(CHILD_MODEL)),
    )
    prepared = prepare_root_usage(scope, values)
    assert len(prepared) == 3
    assert {m.observation.turn_id for m in prepared} == {"root"}


@pytest.mark.parametrize(
    "defect",
    [
        "missing_child",
        "duplicate_child",
        "foreign_session",
        "foreign_account",
        "foreign_tenant",
        "wrong_turn",
        "wrong_thread",
        "session_grain",
        "covers",
        "wrong_model",
        "wrong_price",
        "invalid_cached",
    ],
)
async def test_invalid_or_incomplete_batch_has_no_money_pending_or_outcome(sessions, defect):
    values = measurements()
    child = values[1].observation
    if defect == "missing_child":
        values = values[:1]
    elif defect == "duplicate_child":
        values = (values[1], values[1])
    else:
        updates = {
            "foreign_session": {"session": SESSION.model_copy(update={"id": "other"})},
            "foreign_account": {
                "session": SESSION.model_copy(update={"account_scope_id": "other"})
            },
            "foreign_tenant": {
                "session": SESSION.model_copy(update={"tenant_id": str(uuid.UUID(int=9))})
            },
            "wrong_turn": {"turn_id": "old-child"},
            "wrong_thread": {"thread_id": "other"},
            "session_grain": {"grain": "session"},
            "covers": {"covers": ("openai:turn:root",)},
            "wrong_model": {"model": ROOT_MODEL},
            "invalid_cached": {"input_cached_tokens": 1_000_001},
        }
        child = child.model_copy(update=updates.get(defect, {}))
        values = (
            values[0],
            RootUsageMeasurement(
                child, tariff(ROOT_MODEL) if defect == "wrong_price" else values[1].provider_price
            ),
        )
    outcome = TurnObservation(sessions, TENANT, "slack", session_id=SESSION.id)
    with outcome.activate(), pytest.raises((ValueError, ScopeViolation)):
        await record(sessions, values)
    assert outcome._samples == {}
    assert await balance(sessions) == 0
    assert await mux_state.PostgresStateStore(sessions).pending_outbox(limit=20) == []


async def test_root_and_child_actuals_one_shared_infrastructure_charge_and_outcome(sessions):
    outcome = TurnObservation(sessions, TENANT, "slack", session_id=SESSION.id)
    with outcome.activate():
        assert await record(sessions) == (True, True)
        assert await record(sessions, tuple(reversed(measurements()))) == (False, False)
    assert await balance(sessions) == Decimal("-3.030340")
    outcome.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    async with sessions() as session:
        rows = await list_for_tenant(session, TENANT)
        assert rows[0].model_calls == 2 and rows[0].cost_usd == Decimal("3.030340")
        assert rows[0].input_tokens == 1_000_100 and rows[0].output_tokens == 12
        assert rows[0].model_ids == ["child-model", "root-model"]
        refs = {row["event_id"] for row in rows[0].usage_refs}
        assert refs == {"openai:turn:root", "openai:turn:child"}
        stored = (
            await session.execute(
                text(
                    "SELECT observation_id, applied FROM usage_observation WHERE binding_id='child-binding'"
                )
            )
        ).all()
        assert len(stored) == 2
        assert {row.applied["observation"]["turn_id"] for row in stored} == {"root"}


@pytest.mark.parametrize(
    "unknown", ["infrastructure", "child_tokens", "child_model", "child_tariff"]
)
async def test_unknown_child_charge_is_durable_pending_not_parent_priced_or_free(sessions, unknown):
    values = measurements()
    scope = root_scope(child_model=None) if unknown == "child_model" else root_scope()
    infra = None if unknown == "infrastructure" else Decimal(".03")
    if unknown == "child_tokens":
        values = measurements(
            child=native_usage("child", inputs=None, outputs=None, input_cached_tokens=None)
        )
    if unknown == "child_tariff":
        values = (values[0], replace(values[1], provider_price=None))
    outcome = TurnObservation(sessions, TENANT, "slack", session_id=SESSION.id)
    with outcome.activate():
        result = await record(sessions, values, scope=scope, infrastructure=infra)
    assert result == (False, False) if unknown == "infrastructure" else result == (False, True)
    pending = await mux_state.PostgresStateStore(sessions).pending_outbox(limit=20)
    assert len(pending) == (2 if unknown == "infrastructure" else 1)
    child = next(row.observation for row in pending if row.observation_id == "openai:turn:child")
    assert child.turn_id == "root" and child.thread_id == "subagent"
    if unknown == "child_model":
        assert child.model is None
    assert await balance(sessions) == (0 if infra is None else Decimal("-.030300"))
    outcome.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    async with sessions() as session:
        row = (await list_for_tenant(session, TENANT))[0]
    assert row.cost_usd is None and row.model_calls == 2
    assert row.unpriced_calls == (2 if infra is None else 1)


async def test_pending_native_null_then_measured_revision_settles_and_corrections_replace(sessions):
    unknown = measurements(
        child=native_usage("child", inputs=None, outputs=None, input_cached_tokens=None)
    )
    assert await record(sessions, unknown, scope=root_scope(child_model=None)) == (False, True)
    measured = measurements(child=native_usage("child", revision=2, inputs=1_000_000, outputs=2))
    assert await record(sessions, measured) == (True, False)
    assert await balance(sessions) == Decimal("-3.030340")
    corrected = measurements(child=native_usage("child", revision=3, inputs=900_000, outputs=2))
    assert await record(sessions, corrected) == (True, False)
    assert await balance(sessions) == Decimal("-2.730340")
    assert await record(sessions, corrected) == (False, False)


@pytest.mark.parametrize("revision", [1, 2, 3])
async def test_restart_cannot_move_previously_recorded_child_to_a_new_root(sessions, revision):
    first = measurements(child=native_usage("child", revision=2, inputs=1_000_000, outputs=2))
    await record(sessions, first)
    scope = root_scope(root="new-root")
    values = measurements(
        root=native_usage("new-root", thread_id=None),
        child=native_usage("child", revision=revision, inputs=1_000_000, outputs=2),
    )
    with pytest.raises(ValueError, match="original root attribution"):
        await record(sessions, values, scope=scope)
    assert await balance(sessions) == Decimal("-3.030340")
    async with sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM usage_observation WHERE observation_id='openai:turn:new-root'"
                )
            )
        ) == 0


async def test_shared_infrastructure_remeasurement_requires_new_root_revision(sessions):
    await record(sessions)
    with pytest.raises(ValueError, match="new revision"):
        await record(sessions, infrastructure=Decimal(".04"))
    assert await balance(sessions) == Decimal("-3.030340")
    revised = measurements(root=native_usage(revision=2))
    await record(sessions, revised, infrastructure=Decimal(".04"))
    assert await balance(sessions) == Decimal("-3.040340")


async def test_atomic_child_failure_rolls_back_root_debit_pending_and_capture(
    sessions, monkeypatch
):
    original = tenant_ledger.insert_entry
    calls = 0

    async def crash(*args, **kwargs):
        nonlocal calls
        calls += 1
        await original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("crash after second debit")

    outcome = TurnObservation(sessions, TENANT, "slack", session_id=SESSION.id)
    with outcome.activate(), monkeypatch.context() as patch:
        patch.setattr(tenant_ledger, "insert_entry", crash)
        with pytest.raises(RuntimeError, match="second debit"):
            await record(sessions)
    assert await balance(sessions) == 0 and outcome._samples == {}
    assert await mux_state.PostgresStateStore(sessions).pending_outbox(limit=20) == []
    assert await record(sessions) == (True, True)
    assert await balance(sessions) == Decimal("-3.030340")


async def test_concurrent_reordered_batches_debit_each_native_turn_once(sessions):
    results = await asyncio.wait_for(
        asyncio.gather(record(sessions), record(sessions, tuple(reversed(measurements())))),
        timeout=10,
    )
    assert sorted(results) == [(False, False), (True, True)]
    assert await balance(sessions) == Decimal("-3.030340")


async def test_unverified_disjointness_defaults_to_pending_even_with_known_prices(sessions):
    declared = root_scope()
    scope = RootUsageScope(declared.session, declared.root_turn_id, declared.members)
    assert not scope.disjoint
    assert await record(sessions, scope=scope) == (False, False)
    assert await balance(sessions) == 0
    assert len(await mux_state.PostgresStateStore(sessions).pending_outbox(limit=20)) == 2
    assert await record(sessions, scope=replace(scope, disjoint=True, inventory_complete=True)) == (
        True,
        True,
    )
    assert await balance(sessions) == Decimal("-3.030340")


async def test_unknown_shared_charge_same_revision_measurement_settles_once(sessions):
    assert await record(sessions, infrastructure=None) == (False, False)
    assert await record(sessions) == (True, True)
    assert await record(sessions) == (False, False)
    assert await balance(sessions) == Decimal("-3.030340")


async def test_incomplete_native_inventory_keeps_all_reported_work_pending(sessions):
    assert await record(sessions, scope=root_scope(inventory_complete=False)) == (False, False)
    assert await balance(sessions) == 0
    assert len(await mux_state.PostgresStateStore(sessions).pending_outbox(limit=20)) == 2
    assert await record(sessions) == (True, True)
    assert await balance(sessions) == Decimal("-3.030340")


async def test_competing_roots_cannot_capture_same_child_even_concurrently(sessions):
    async def claimed(root):
        return await record(
            sessions,
            measurements(root=native_usage(root, thread_id=None)),
            scope=root_scope(root=root),
        )

    results = await asyncio.wait_for(
        asyncio.gather(claimed("root-a"), claimed("root-b"), return_exceptions=True), timeout=10
    )
    assert sum(result == (True, True) for result in results) == 1
    refused = [result for result in results if isinstance(result, ValueError)]
    assert len(refused) == 1 and "original root attribution" in str(refused[0])
    assert await balance(sessions) == Decimal("-3.030340")


async def test_same_revision_cannot_invent_child_model_after_unknown_attribution(sessions):
    await record(sessions, scope=root_scope(child_model=None))
    with pytest.raises(UsageRevisionConflict):
        await record(sessions)
    assert await balance(sessions) == Decimal("-.030300")
    revised = measurements(child=native_usage("child", revision=2, inputs=1_000_000, outputs=2))
    await record(sessions, revised)
    assert await balance(sessions) == Decimal("-3.030340")


async def test_unverified_higher_revision_does_not_reuse_prior_outcome_tariff(sessions):
    outcome = TurnObservation(sessions, TENANT, "slack", session_id=SESSION.id)
    with outcome.activate():
        await record(sessions)
        revised = measurements(child=native_usage("child", revision=2, inputs=1_000_100, outputs=2))
        assert await record(sessions, revised, scope=root_scope(disjoint=False)) == (False, False)
    assert await balance(sessions) == Decimal("-3.030340")
    outcome.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    async with sessions() as session:
        row = (await list_for_tenant(session, TENANT))[0]
    assert row.cost_usd is None and row.unpriced_calls == 1


async def test_a_later_complete_inventory_cannot_drop_known_child_usage(sessions):
    declared = root_scope()
    expanded = replace(
        declared,
        members=(
            *declared.members,
            RootUsageMember("grandchild", "child", "nested-agent", CHILD_MODEL),
        ),
    )
    values = (
        *measurements(),
        RootUsageMeasurement(
            native_usage("grandchild", thread_id="nested-agent", inputs=20), tariff(CHILD_MODEL)
        ),
    )
    await record(sessions, values, scope=expanded)
    before = await balance(sessions)
    with pytest.raises(ValueError, match="omit previously recorded native work"):
        await record(sessions)
    assert await balance(sessions) == before


async def test_stale_same_root_snapshot_cannot_rewind_capture_or_charge(sessions):
    revised = measurements(child=native_usage("child", revision=2, inputs=900_000, outputs=2))
    await record(sessions, revised)
    with pytest.raises(ValueError, match="latest durable revisions"):
        await record(sessions)
    assert await balance(sessions) == Decimal("-2.730340")


async def test_active_outcome_cannot_mix_a_second_root_before_any_write(sessions):
    outcome = TurnObservation(sessions, TENANT, "slack", session_id=SESSION.id)
    with outcome.activate():
        await record(sessions)
        values = measurements(
            root=native_usage("new-root", thread_id=None), child=native_usage("new-child")
        )
        with pytest.raises(ValueError, match="active outcome attribution"):
            await record(sessions, values, scope=root_scope(root="new-root", child="new-child"))
    assert len(outcome._samples) == 2
    assert await balance(sessions) == Decimal("-3.030340")


def test_gemini_interaction_totals_are_not_assumed_to_expose_disjoint_child_work():
    declared = root_scope()
    with pytest.raises(ValueError, match="OpenAI native session"):
        RootUsageScope(SESSION.model_copy(update={"provider": "gemini"}), "root", declared.members)


async def test_sdk_mocktransport_retains_root_and_large_child_usage_at_root(sessions):
    requests = []
    native = [
        {
            "id": "root",
            "session_id": SESSION.id,
            "subagent_id": None,
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 10,
            },
        },
        {
            "id": "child",
            "session_id": SESSION.id,
            "subagent_id": "subagent",
            "usage": {
                "input_tokens": 1_000_000,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 2,
            },
        },
    ]
    scope = Scope(
        tenant_id=str(TENANT),
        account_id="account",
        principal_id="user",
        authorization_id="offline-native-grant",
    )

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path.endswith(f"/{SESSION.id}/turns")
        return httpx.Response(200, json={"data": native, "has_more": False, "last_id": "child"})

    async with AsyncOpenAI(
        api_key="offline",
        organization="org-offline",
        project="project",
        webhook_secret="offline",
        base_url="https://api.openai.com/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda s, k, i: s == scope,
        )
        observations = await driver.usage.reconcile(scope, SESSION)
    assert len(requests) == 1 and len(observations) == 2
    values = tuple(
        RootUsageMeasurement(
            value, tariff(ROOT_MODEL) if value.thread_id is None else tariff(CHILD_MODEL, "3", "20")
        )
        for value in observations
    )
    await record(sessions, values)
    assert await balance(sessions) == Decimal("-3.030340")


async def second_binding(sessions):
    await mux_state.PostgresStateStore(sessions).put_binding(
        ProviderBinding(
            id="second-binding",
            thread=ThreadRef(
                channel=ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel"),
                thread_id="second-thread",
            ),
            provider="openai",
            profile="openai.persistent_workspace",
            native_refs={},
            generation=1,
            config_revision=1,
        ),
        expected_generation=0,
    )


@pytest.mark.parametrize("revision", [1, 2, 3])
async def test_native_work_cannot_open_another_binding_correction_history(sessions, revision):
    await record(sessions)
    await second_binding(sessions)
    revised = measurements(
        child=native_usage("child", revision=revision, inputs=900_000, outputs=2)
    )
    with pytest.raises(ScopeViolation, match="does not own the observed session"):
        await record_root_usage(
            sessionmaker=sessions,
            binding_id="second-binding",
            scope=root_scope(),
            measurements=revised,
            tenant_id=TENANT,
            platform_user_id="user",
            infrastructure_usd=Decimal(".03"),
            channel_id="channel",
        )
    assert await balance(sessions) == Decimal("-3.030340")
    async with sessions() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM usage_observation WHERE binding_id='second-binding'")
            )
            == 0
        )


async def test_concurrent_first_batches_across_bindings_charge_native_work_once(sessions):
    await second_binding(sessions)
    results = await asyncio.gather(
        record(sessions),
        record_root_usage(
            sessionmaker=sessions,
            binding_id="second-binding",
            scope=root_scope(),
            measurements=measurements(),
            tenant_id=TENANT,
            platform_user_id="user",
            infrastructure_usd=Decimal(".03"),
            channel_id="channel",
        ),
        return_exceptions=True,
    )
    assert sum(result == (True, True) for result in results) == 1
    failures = [result for result in results if isinstance(result, ScopeViolation)]
    assert len(failures) == 1 and "does not own the observed session" in str(failures[0])
    assert await balance(sessions) == Decimal("-3.030340")
