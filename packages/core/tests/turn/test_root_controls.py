"""Controlled native roots keep one original hold and count real input claims."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from uuid import UUID, uuid4

import httpx
import pytest
from daimon.core._models import ProviderBindingSlot
from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendService, BackendActor
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.stores import mux_state
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.prepare import PreparedTurn, bind_recorder, create_ma_session
from daimon.core.turn.run import _replace_dead_session_locked  # pyright: ignore[reportPrivateUsage]
from daimon.core.turn.runtime_manifest import (
    prepare_controlled_root,
    root_claim_check,
    run_controlled_preparation,
)
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.receipts import CancelReceipt, Operation, SendReceipt
from mux.state.store import binding_slot
from sqlalchemy import delete, text

from .test_frozen_preparation import revoke
from .test_frozen_root import root_turn as root_turn
from .test_root_funding import credit


async def select_controls(value, *, before_native=None):
    deps, prepared, _, intent, _, _, binding, _ = value
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    grant = await service.read_fleet_grant(source.tenant_id, source.agent_id, now=datetime.now(UTC))
    grant = grant.model_copy(
        update={"native_root_budget_usd": Decimal("0.03"), "root_cash_budget_usd": Decimal("0.06")}
    )
    preview = await service.preview_fleet_grant(source, grant, now=datetime.now(UTC))
    await service.set_fleet_grant(
        source,
        grant,
        preview.setting_revision,
        BackendActor(source.tenant_id, prepared.admission.account_id, Subject(is_admin=True)),
        expected_preview_digest=preview.digest,
        now=datetime.now(UTC),
    )
    await credit(value)
    # The fixture has an acknowledged session; remove only its owned slot to
    # exercise the real preallocation selector, then restore it for send tests.
    async with deps.sessionmaker.begin() as db:
        await db.execute(
            delete(ProviderBindingSlot).where(
                ProviderBindingSlot.binding_id == binding.id,
                ProviderBindingSlot.tenant_id == source.tenant_id,
            )
        )
    request = replace(
        prepared._preparation,
        deps=replace(deps, turn_path="mux"),
        before_native=before_native,
    )
    return await prepare_controlled_root(request, intent.id)


async def root_record(value):
    deps, _, _, _, origin, scope, *_ = value
    async with deps.sessionmaker() as db:
        return await mux_state.get_fleet_root_operation(db, scope, origin_id=origin.id)


async def send(value, request, key, *, observer=None, kind="send"):
    deps, prepared, _, _, origin, scope, binding, _ = value
    persistence = TurnPersistence(deps.state_store, binding, scope, operation_key=key)
    persistence.root_claim = partial(
        root_claim_check, deps, origin.id, request.root_controls, request.before_native
    )
    inputs = [UserMessage(content=(TextPart(text=key),))]

    async def pump():
        with persistence.activate():
            return await persistence.mutate(
                prepared.session_ref,
                kind,
                [item.model_dump(mode="json") for item in inputs],
                lambda operation_key: prepared.backend.events.send(
                    scope, prepared.session_ref, inputs, key=operation_key
                ),
                SendReceipt,
                lambda receipt: "processed",
            )

    if observer is None:
        return await persistence.run(pump)
    with observer.activate():
        return await persistence.run(pump)


async def test_caller_guard_identity_is_composed_and_refuses_before_native_allocation(root_turn):
    callback_calls = []

    async def caller():
        callback_calls.append(caller)
        raise WorkerSpecRefused("scope", "stale")

    request = await select_controls(root_turn, before_native=caller)
    transport = root_turn[-1]
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused):
        await run_controlled_preparation(
            request,
            create_ma_session(
                request.deps,
                request.admission,
                tenant_id=request.tenant_id,
                preparation=request,
                platform="discord",
            ),
        )
    assert callback_calls == [caller]
    # A root is an ordinary agent session: it keeps main's personal memory store. That store is
    # provisioned before the guard refuses, and it is neither a counted input nor funded.
    non_get = [(r.method, r.path) for r in transport.requests[before:] if r.method != "GET"]
    assert non_get == [("POST", "/v1/memory_stores")]
    root = await root_record(root_turn)
    assert root.result["root_finance"]["input_keys"] == []
    assert root.result["root_finance"]["exposure_usd"] == "0.06"


async def test_a_root_without_a_native_cap_keeps_the_host_hold_and_no_native_budget(
    root_turn, monkeypatch
):
    """A provider with no native session cap (OpenAI on our project: `spend_control`
    is a 400) still gets the grant's root controls and cash hold, never a native budget."""
    from daimon.core.turn.prepare import _admitted_profile  # pyright: ignore[reportPrivateUsage]
    from mux import profiles

    _, prepared, *_ = root_turn
    profile_id = _admitted_profile(prepared._preparation.admission)
    original = profiles.PROFILES[profile_id]
    monkeypatch.setitem(
        profiles.PROFILES,
        profile_id,
        original.model_copy(
            update={"support": {**original.support, "session_spend_control": "unsupported"}}
        ),
    )
    request = await select_controls(root_turn)
    assert request.native_budget_usd is None
    assert request.root_controls is not None
    assert request.root_controls["cash_budget_usd"] == "0.06"
    # The grant's figure only sizes the hold here; nothing records it as a native cap.
    assert "native_budget_usd" not in request.root_controls
    assert request.root_controls["hold_budget_usd"] == "0.03"
    root = await root_record(root_turn)
    assert root.result["root_finance"]["exposure_usd"] == "0.06"


async def test_three_actual_sdk_inputs_and_replay_retain_first_outcome(root_turn):
    deps, prepared, _, _, _, scope, binding, transport = root_turn
    request = await select_controls(root_turn)
    await deps.state_store.put_binding(binding, expected_generation=0)
    before = len(transport.requests)
    ids = []
    for number in range(3):
        observer = TurnObservation(
            deps.sessionmaker,
            UUID(scope.tenant_id),
            "discord",
            account_id=UUID(scope.account_id),
            session_id=prepared.ma_session_id,
        )
        ids.append(str(observer.id))
        await send(root_turn, request, f"counted-{number}", observer=observer)
    original = await root_record(root_turn)
    assert list(original.result["root_finance"]["input_outcomes"].values()) == ids
    await send(root_turn, request, "counted-0")
    with pytest.raises(WorkerSpecRefused) as denied:
        await send(root_turn, request, "fourth")
    assert denied.value.field == "max_turns"
    posts = [r for r in transport.requests[before:] if r.method == "POST"]
    assert len(posts) == 3
    assert all(len(r.json()["events"]) == 1 for r in posts)
    assert await root_record(root_turn) == original


@pytest.mark.parametrize("kind", ["cancel", "archive", "recovery-interrupt"])
async def test_revoked_grant_refuses_at_claim_but_drain_bypasses_it(root_turn, kind):
    deps, prepared, _, _, _, _, binding, transport = root_turn
    request = await select_controls(root_turn)
    await deps.state_store.put_binding(binding, expected_generation=0)
    await revoke(root_turn)
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused):
        await send(root_turn, request, "after-revoke")
    assert len(transport.requests) == before
    assert (await root_record(root_turn)).result["root_finance"]["input_keys"] == []
    persistence = TurnPersistence(deps.state_store, binding, request.scope, operation_key=kind)
    persistence.root_claim = partial(
        root_claim_check,
        deps,
        UUID(request.root_controls["origin_id"]),
        request.root_controls,
        request.before_native,
    )
    if kind == "archive":
        transport.router.add(
            "POST",
            rf"/v1/sessions/{prepared.ma_session_id}/archive",
            lambda request, match: httpx.Response(200, json={}),
        )

    async def pump():
        with persistence.activate():
            if kind == "archive":
                return await persistence.mutate(
                    prepared.session_ref,
                    kind,
                    {},
                    lambda operation_key: prepared.backend.sessions.archive(
                        request.scope, prepared.session_ref, key=operation_key
                    ),
                    Operation,
                    lambda receipt: "processed",
                )
            return await persistence.mutate(
                prepared.session_ref,
                kind,
                {"type": "user.interrupt"},
                lambda operation_key: prepared.backend.events.cancel(
                    request.scope, prepared.session_ref, turn_id="drain", key=operation_key
                ),
                CancelReceipt,
                lambda receipt: "accepted",
            )

    await persistence.run(pump)
    posts = [r for r in transport.requests[before:] if r.method == "POST"]
    assert len(posts) == 1
    if kind != "archive":
        assert posts[0].json()["events"] == [{"type": "user.interrupt"}]
    assert (await root_record(root_turn)).result["root_finance"]["input_keys"] == []


async def test_real_root_allocation_carries_native_budget_and_replays_ack(root_turn):
    from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_TENANT
    from mux.testing.drivers.anthropic.ma import make_fake_memory_store_handler
    from mux.testing.drivers.anthropic.ma_models import ma_session
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    callback_calls = []

    async def caller():
        callback_calls.append(caller)

    request = await select_controls(root_turn, before_native=caller)
    transport = root_turn[-1]
    native = ma_session(
        id="fresh-controlled-root",
        agent_id=request.admission.agent.id,
        model=request.admission.agent.model.id,
        environment_id=request.admission.environment.id,
        metadata={
            MA_METADATA_KEY_TENANT: str(request.tenant_id),
            MA_METADATA_KEY_ACCOUNT: str(request.admission.account_id),
        },
    ).model_dump(mode="json")
    native["budget"] = {"type": "limit", "max_list_cost": {"amount": "3", "currency": "USD"}}
    transport.queue(
        ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=native)),
    )
    transport.router.add(
        "GET",
        r"/v1/sessions/fresh-controlled-root",
        lambda request, match: httpx.Response(200, json=native),
    )
    # The root keeps main's personal memory store; answer its provisioning like other session tests.
    memory = make_fake_memory_store_handler()
    transport.router.add("POST", r"/v1/memory_stores", lambda request, match: memory(request))
    before = len(transport.requests)

    async def create():
        return await create_ma_session(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            preparation=request,
            platform="discord",
        )

    first = await run_controlled_preparation(request, create())
    second = await run_controlled_preparation(request, create())
    assert first == second
    posts = [r for r in transport.requests[before:] if r.method == "POST"]
    # The personal memory store is provisioned once (ordinary agent session); only the session
    # carries the native budget, and the store is neither a counted input nor funded.
    assert [r.path for r in posts] == ["/v1/memory_stores", "/v1/sessions"]
    assert "budget" not in posts[0].json()
    assert posts[1].json()["budget"] == native["budget"]
    assert callback_calls and all(c is caller for c in callback_calls)
    root = await root_record(root_turn)
    assert root.result["root_finance"]["input_keys"] == []
    assert root.result["root_finance"]["exposure_usd"] == "0.06"


async def test_host_turn_freezes_the_same_reservation_and_counts_before_native_input(root_turn):
    import asyncio

    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.runtime_manifest import read_frozen_root
    from daimon.testing.turn_fakes import RecordingLifecycle

    deps, prepared, _, intent, origin, scope, binding, transport = root_turn
    request = await select_controls(root_turn)
    await deps.state_store.put_binding(binding, expected_generation=0)
    prepared = replace(prepared, _preparation=request)
    before = len(transport.requests)

    async def reseed():
        return "Root input"

    await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=request.tenant_id,
        platform="discord",
        thread_id=request.thread_id,
        external_user_id=request.external_user_id,
        user_message="Root input",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        turn_card_intent_id=intent.id,
        operation_key="host-controlled",
    )
    record = await root_record(root_turn)
    assert record.operation.status == "processed"
    assert record.result["root_controls"] == request.root_controls
    assert record.result["root_finance"]["input_keys"] == [
        f"turn:{prepared.ma_session_id}:host-controlled:send:0"
    ]
    assert record.result["root_finance"]["exposure_usd"] == "0.06"
    retained, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    assert str(retained.fleet.run_id) == record.result["permissions"]["fleet"]["run_id"]
    assert len([r for r in transport.requests[before:] if r.method == "POST"]) == 1


@pytest.mark.parametrize("fault", ["undersized", "balance", "epoch"])
async def test_claim_rechecks_original_funding_and_current_grant(root_turn, fault):
    from daimon.core.fleet import FleetConflict
    from daimon.core.stores import tenant_ledger

    deps, prepared, _, _, origin, scope, binding, transport = root_turn
    request = await select_controls(root_turn)
    await deps.state_store.put_binding(binding, expected_generation=0)
    await request.before_native()
    if fault == "undersized":
        original = await root_record(root_turn)
        async with deps.sessionmaker.begin() as db:
            await mux_state.update_fleet_root_finance(
                db,
                scope,
                origin_id=origin.id,
                root_finance={**original.result["root_finance"], "exposure_usd": "0.01"},
            )
    elif fault == "balance":
        async with deps.sessionmaker.begin() as db:
            await tenant_ledger.insert_entry(
                db,
                tenant_id=UUID(scope.tenant_id),
                delta_usd=Decimal("-0.01"),
                reason="test",
                idempotency_key="concurrent-root-debit",
            )
    else:
        source = prepared._source
        service = AgentBackendService(deps.sessionmaker, enabled=True)
        value = await service.read_fleet_grant(
            source.tenant_id, source.agent_id, now=datetime.now(UTC)
        )
        preview = await service.preview_fleet_grant(source, value, now=datetime.now(UTC))
        await service.set_fleet_grant(
            source,
            value,
            preview.setting_revision,
            BackendActor(source.tenant_id, prepared.admission.account_id, Subject(is_admin=True)),
            expected_preview_digest=preview.digest,
            now=datetime.now(UTC),
        )
    before = len(transport.requests)
    with pytest.raises((FleetConflict, WorkerSpecRefused)):
        await send(root_turn, request, "fresh-after-change")
    assert len(transport.requests) == before
    assert (await root_record(root_turn)).result["root_finance"]["input_keys"] == []


async def test_original_store_claim_authority_is_not_replaced_by_root_controls(root_turn):
    from mux.errors import ScopeViolation

    deps, _, _, _, _, _, binding, transport = root_turn
    request = await select_controls(root_turn)
    checks = []

    async def caller(db, scope, key, fence):
        checks.append(caller)
        return False

    store = mux_state.PostgresStateStore(deps.sessionmaker, before_claim=caller)
    await store.put_binding(binding, expected_generation=0)
    changed = (replace(deps, state_store=store), *root_turn[1:])
    before = len(transport.requests)
    with pytest.raises(ScopeViolation):
        await send(changed, request, "denied-by-owner")
    assert checks == [caller]
    assert len(transport.requests) == before
    assert (await root_record(root_turn)).result["root_finance"]["input_keys"] == []


async def test_two_completion_inputs_use_the_original_root_hold_and_fourth_refuses(root_turn):
    from daimon.core.turn.runtime_manifest import read_frozen_root

    from .test_frozen_root import completion_claim, run_root_input, wake_frame

    deps, prepared, _, _, _, scope, binding, transport = root_turn
    request = await select_controls(root_turn)
    await deps.state_store.put_binding(binding, expected_generation=0)
    value = (request.deps, replace(prepared, _preparation=request), *root_turn[2:])
    await run_root_input(value)
    original = await root_record(value)
    parent, _ = await read_frozen_root(
        deps, scope, origin_id=root_turn[4].id, now=datetime.now(UTC)
    )
    previous = None
    for ordinal in range(2):
        row, worker = await completion_claim(value, ordinal=ordinal, reviewer_of=previous)
        async with wake_frame(value) as (intent, origin):
            await run_root_input(value, intent_id=intent.id, completion=row)
            carried, _ = await read_frozen_root(
                deps, scope, origin_id=origin.id, now=datetime.now(UTC)
            )
            assert carried == parent
        previous = worker.id
    after = await root_record(value)
    assert after.operation.key == original.operation.key
    assert after.result["root_controls"] == original.result["root_controls"]
    assert after.result["root_finance"]["exposure_usd"] == "0.06"
    assert len(after.result["root_finance"]["input_keys"]) == 3
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused) as denied:
        await run_root_input(value)
    assert denied.value.field == "max_turns"
    assert not any(r.method == "POST" for r in transport.requests[before:])
    async with deps.sessionmaker() as db:
        rows = await mux_state.list_fleet_root_finances(db, tenant_id=UUID(scope.tenant_id))
    assert len(rows) == 1 and rows[0] == after


async def test_busy_preallocation_closes_unstarted_native_preparation_without_io(root_turn):
    from datetime import timedelta

    from mux.state.lease import LeaseBusy

    request = await select_controls(root_turn)
    store = request.deps.state_store
    slot = binding_slot(root_turn[6])
    lease = await store.acquire_lease(
        slot, holder="other-root", turn_id="other-root", now=request.now(), ttl=timedelta(minutes=5)
    )
    preparing = create_ma_session(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        preparation=request,
        platform="discord",
    )
    before = len(root_turn[-1].requests)
    try:
        with pytest.raises(LeaseBusy):
            await run_controlled_preparation(request, preparing)
    finally:
        await store.release_lease(lease)
    assert preparing.cr_frame is None
    assert len(root_turn[-1].requests) == before
    assert (await root_record(root_turn)).result["root_finance"]["input_keys"] == []


async def test_controlled_recovery_refuses_successor_native_allocation(root_turn):
    """A dead native-root-controlled session gets no fresh session/budget.

    `_replace_dead_session_locked` must refuse before touching policy, the
    advisory lock or `create_ma_session` the moment it sees a preparation
    carrying `native_budget_usd` -- recovery is a fresh-session mechanism for
    the ordinary path, not a way to mint a second controlled allocation for
    one original root hold.
    """
    deps, prepared, _, _, _, _, _, transport = root_turn
    request = await select_controls(root_turn)
    assert request.native_budget_usd is not None
    record = bind_recorder(
        deps,
        tenant_id=request.tenant_id,
        external_user_id=request.external_user_id,
        ma_session_id=prepared.ma_session_id,
        model_id=prepared.admission.agent.model.id,
    )
    controlled = PreparedTurn(
        admission=prepared.admission,
        ma_session_id=prepared.ma_session_id,
        mapping_id=prepared.mapping_id,
        watermark=None,
        reused=True,
        session_account_id=prepared.session_account_id,
        _record=record,
        _preparation=request,
    )
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused) as refused:
        await _replace_dead_session_locked(
            deps,
            controlled,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            dead_session_id=prepared.ma_session_id,
            dead_mapping_id=UUID(int=0),
        )
    assert refused.value.field == "native_budget_usd"
    assert transport.requests == transport.requests[:before]


async def test_claim_refuses_once_database_clock_passes_prep_deadline(root_turn):
    """The claim-time deadline recheck (`runtime_manifest.py:477-478`) compares
    `controls["deadline"]` against the REAL database clock -- a distinct guard
    from `guard()`'s own `remaining_s`/grant-digest recheck, which both run on
    the python-side `request.now()`. Hold the grant/epoch/digest fixed and give
    `request.now()` a fixed reading that never trips `remaining_s`, but pin the
    prep's own deadline far in the past relative to the database's real wall
    clock: only the explicit `database_clock(db)` comparison can catch that.
    """
    deps, prepared, _, intent, _, scope, binding, transport = root_turn
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    grant = await service.read_fleet_grant(source.tenant_id, source.agent_id, now=datetime.now(UTC))
    grant = grant.model_copy(
        update={"native_root_budget_usd": Decimal("0.03"), "root_cash_budget_usd": Decimal("0.06")}
    )
    preview = await service.preview_fleet_grant(source, grant, now=datetime.now(UTC))
    await service.set_fleet_grant(
        source,
        grant,
        preview.setting_revision,
        BackendActor(source.tenant_id, prepared.admission.account_id, Subject(is_admin=True)),
        expected_preview_digest=preview.digest,
        now=datetime.now(UTC),
    )
    await credit(root_turn)
    async with deps.sessionmaker.begin() as db:
        await db.execute(
            delete(ProviderBindingSlot).where(
                ProviderBindingSlot.binding_id == binding.id,
                ProviderBindingSlot.tenant_id == source.tenant_id,
            )
        )
    frozen_now = datetime(2000, 1, 1, tzinfo=UTC)
    tight_deadline = frozen_now + timedelta(seconds=30)
    base_request = replace(
        prepared._preparation,
        deps=replace(deps, turn_path="mux"),
        deadline=tight_deadline,
        now=lambda: frozen_now,
    )
    request = await prepare_controlled_root(base_request, intent.id)
    assert request.root_controls is not None
    assert request.root_controls["deadline"] == tight_deadline.isoformat()

    await deps.state_store.put_binding(binding, expected_generation=0)
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused) as denied:
        await send(root_turn, request, "claimed-after-db-deadline")
    assert (denied.value.field, denied.value.reason) == ("origin", "stale")
    assert len(transport.requests) == before, "no native POST must escape a stale-deadline refusal"
    root = await root_record(root_turn)
    assert root.result["root_finance"]["input_keys"] == []
    assert root.result["root_finance"]["exposure_usd"] == "0.06"


async def test_crash_resumed_claim_retains_first_observer_outcome_without_double_write(root_turn):
    """First-observer retention and the `if fresh:` write gate around
    `update_fleet_root_finance` (`runtime_manifest.py:498-513`).

    Simulates a crash/resume: `root_claim_check` already wrote the key's
    `input_keys`/`input_outcomes` entry for an earlier observer, but the
    native POST that key corresponds to never confirmed (so nothing short
    circuits the retry at the `TurnPersistence` layer). Driving a claim for
    that SAME key with a DIFFERENT current observer active must retain the
    original outcome untouched and must not duplicate the key or re-debit --
    only the `if fresh:` gate stops a second write.
    """
    deps, prepared, _, _, origin, scope, binding, transport = root_turn
    request = await select_controls(root_turn)
    await deps.state_store.put_binding(binding, expected_generation=0)

    operation_key = "crash-resume-key"
    # `TurnPersistence.mutate` namespaces the claim key it hands `root_claim_check`
    # as `f"{operation_key}:{kind}:{attempt}"` -- match that so the seeded entry
    # lines up with the key the resumed `send()` below will actually claim.
    key = f"{operation_key}:send:0"
    first_outcome_id = uuid4()
    original = await root_record(root_turn)
    finance = original.result["root_finance"]
    assert finance["input_keys"] == []
    async with deps.sessionmaker.begin() as db:
        await mux_state.update_fleet_root_finance(
            db,
            scope,
            origin_id=origin.id,
            root_finance={
                **finance,
                "input_keys": [key],
                "input_outcomes": {key: str(first_outcome_id)},
            },
        )
    seeded = await root_record(root_turn)
    assert seeded.result["root_finance"]["input_outcomes"][key] == str(first_outcome_id)

    different_observer = TurnObservation(
        deps.sessionmaker,
        UUID(scope.tenant_id),
        "discord",
        account_id=UUID(scope.account_id),
        session_id=prepared.ma_session_id,
    )
    assert str(different_observer.id) != str(first_outcome_id)

    before = len(transport.requests)
    await send(root_turn, request, operation_key, observer=different_observer)
    posts = [r for r in transport.requests[before:] if r.method == "POST"]
    assert len(posts) == 1, "the resumed (previously-unconfirmed) native send must still go out"

    record = await root_record(root_turn)
    finance_after = record.result["root_finance"]
    assert finance_after["input_keys"] == [key], "resumed claim must not duplicate the counted key"
    assert finance_after["input_outcomes"] == {key: str(first_outcome_id)}, (
        "first-observer outcome must survive a resumed claim under a different current observer"
    )
    assert finance_after["exposure_usd"] == finance["exposure_usd"], "no extra debit on resume"


async def test_the_claim_guard_runs_without_holding_the_tenant_fleet_lock(root_turn):
    """SMOKE J4: the send claim held the fleet lock (and the tenant row) while the guard
    read the grant on other sessions, so every fleet admission in the tenant waited."""
    deps, _, _, _, _, scope, binding, _ = root_turn
    free: list[bool] = []

    async def probe() -> None:
        async with deps.sessionmaker.begin() as db:
            free.append(
                (
                    await db.execute(
                        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": f"fleet:{scope.tenant_id}"},
                    )
                ).scalar_one()
            )

    request = await select_controls(root_turn, before_native=probe)
    await deps.state_store.put_binding(binding, expected_generation=0)
    free.clear()
    await send(root_turn, request, "guarded")
    assert free and all(free)
