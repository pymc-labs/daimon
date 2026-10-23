"""The installed fleet caller reaches the existing real SDK worker driver."""

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from daimon.core._models import FleetWorker as WorkerRow
from daimon.core.errors import TurnError
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused, HostFleetPorts
from daimon.core.headless_runner import run_worker
from daimon.core.mux_compat import retrieve_agent
from daimon.core.neutral_resources import WorkerSpecRefused, resolve_worker_spec
from daimon.core.stores import access_policy, fleet, mux_state
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.runtime_manifest import capture_preparation_source
from mux.contracts.resources import AgentPatch
from mux.testing.drivers.anthropic.ma import MARouter
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from sqlalchemy import update

from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn
from .test_openai_worker_plan import application as application
from .test_openai_worker_plan import claimed_openai_worker as claimed_openai_worker
from .test_openai_worker_plan import db_preflight as db_preflight
from .test_openai_worker_plan import db_session_factory as db_session_factory
from .test_openai_worker_plan import owned as owned
from .test_openai_worker_plan import owned_database_preflight as owned_database_preflight
from .test_openai_worker_plan import provider as provider

root_turn_factory = root_turn.__wrapped__


@pytest.fixture
async def worker_root(claimed_openai_worker):
    request, root, row, prices, _, calls, wire = claimed_openai_worker
    request = replace(request, scope=root.fleet.scope, worker=None, source=None)
    source = request.admission.agent.model_dump(mode="json")
    router = MARouter()
    router.add(
        "GET",
        re.escape("/v1/agents/" + request.admission.agent.id),
        lambda call, match: httpx.Response(200, json=source),
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        actual = await retrieve_agent(client, request.admission.agent.id, scope=request.scope)
        request = replace(
            request,
            deps=replace(request.deps, anthropic=client),
            admission=replace(request.admission, agent=actual),
        )
        captured = await capture_preparation_source(request)
        assert captured.source is not None
        root = replace(
            root,
            source_agents=(captured.source.ref,),
            source_revisions=((captured.source.ref, captured.source.revision),),
        )
        previous = claimed_openai_worker[0].worker
        resolved = resolve_worker_spec(
            root,
            captured.source if previous.source_agent else previous.spec,
            overrides=AgentPatch.model_validate(previous.spec.model_dump(mode="json"))
            if previous.source_agent
            else None,
            policy=await _policy(request),
            now=request.now(),
            backend=previous.backend,
            limits=previous.limits,
            resources=previous.resources,
            environment=previous.environment,
            captured_source=captured,
        )
        # Seed the admitted intent from the actual SDK source read, as a real root does.
        async with request.deps.sessionmaker.begin() as db:
            await db.execute(
                update(WorkerRow)
                .where(WorkerRow.id == row.id)
                .values(spec=resolved.spec.model_dump(mode="json"), spec_digest=resolved.digest)
            )
        _, rows = await fleet.run_status(
            request.deps.sessionmaker, tenant_id=row.tenant_id, run_id=row.run_id
        )
        yield request, root, rows[0], resolved, prices, calls, wire, transport, source


async def _policy(request):
    async with request.deps.sessionmaker() as db:
        return await access_policy.load_access_policy(db, tenant_id=request.tenant_id)


def _spawn_terms(request, root, resolved, key):
    from mux.contracts.config import BackendConfig

    clone = resolved.source_agent
    return dict(
        now=request.now(),
        run_id=root.fleet.run_id,
        operation_key=key,
        role="worker",
        backend=BackendConfig.model_validate(resolved.backend.model_dump(mode="json")),
        agent=resolved.spec if clone is None else None,
        clone=clone[0] if clone else None,
        clone_revision=clone[1] if clone else None,
        overrides=AgentPatch.model_validate(resolved.spec.model_dump(mode="json"))
        if clone
        else None,
        limits=resolved.limits,
    )


async def _settled(ports, root, identifier):
    async with asyncio.timeout(30):
        while True:
            _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
            selected = next(row for row in rows if str(row.id) == identifier)
            if selected.status in ("failed", "succeeded", "cancelled"):
                return selected
            await asyncio.sleep(0.01)


async def test_root_request_derivation_reaches_real_fresh_or_clone_worker(worker_root):
    request, root, row, resolved, prices, calls, _, transport, _ = worker_root
    request = replace(request, native_budget_usd=Decimal("0.03"), root_controls={"max_inputs": 3})
    derived = await request.for_worker(root, resolved)
    assert derived.admission.grant is request.admission.grant
    assert derived.scope == resolved.permissions.fleet.scope
    assert derived.deadline == min(request.deadline, root.limits.deadline, resolved.limits.deadline)
    assert derived.root_controls is None and derived.native_budget_usd is None
    assert derived.counterpart is None and derived.verified_counterpart is None
    result = await run_worker(
        derived,
        parent=root,
        worker=row,
        trigger_message="Complete the task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded" and result.result is not None
    assert [call.url.path for call in calls if call.method == "POST"] == [
        "/v1/agents",
        "/v1/agents/sessions",
        "/v1/agents/sessions/native-openai-session/events",
    ]
    assert all(call.method == "GET" for call in transport.requests)


@pytest.mark.parametrize(
    "fault", ("subject", "place", "scope", "pin", "expired", "stale", "disabled", "slow")
)
async def test_derivation_refuses_unbound_stale_expired_and_disabled_before_revision_or_allocation(
    worker_root, fault
):
    request, root, _, resolved, _, calls, _, transport, source = worker_root
    async with request.deps.sessionmaker() as db:
        before = await mux_state.effective_revision(
            db, root.fleet.root.channel, resolved.backend.content_digest(), "agent"
        )
    reads_before = list(transport.requests)
    if fault == "subject":
        root = replace(root, subject=replace(root.subject, platform_user_id="other"))
    elif fault == "place":
        root = replace(
            root, fleet=replace(root.fleet, place=replace(root.fleet.place, channel_id="other"))
        )
    elif fault == "scope":
        request = replace(request, scope=request.scope.model_copy(update={"account_id": "other"}))
    elif fault == "pin":
        root = replace(root, source_revisions=())
    elif fault == "expired":
        request = replace(request, deadline=request.now() - timedelta(seconds=1))
    elif fault == "stale":
        source["version"] += 1
    elif fault == "disabled":
        async with request.deps.sessionmaker.begin() as db:
            policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
            await access_policy.set_access_policy(
                db,
                tenant_id=request.tenant_id,
                policy=policy.model_copy(update={"enabled_backends": ("anthropic",)}),
            )
    else:
        request = replace(request, deadline=request.now() + timedelta(milliseconds=30))

        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(1)
                yield b"{}"

        transport.router.routes.clear()
        transport.router.add(
            "GET",
            re.escape("/v1/agents/" + request.admission.agent.id),
            lambda call, match: httpx.Response(200, stream=Slow()),
        )
    expected = (
        TurnError if fault == "expired" else TimeoutError if fault == "slow" else WorkerSpecRefused
    )
    with pytest.raises(expected):
        await request.for_worker(root, resolved)
    if fault in ("subject", "place", "scope", "pin", "expired"):
        assert transport.requests == reads_before
    assert not calls
    async with request.deps.sessionmaker() as db:
        assert (
            await mux_state.effective_revision(
                db, root.fleet.root.channel, resolved.backend.content_digest(), "agent"
            )
            == before
        )


async def test_real_orchestrator_dispatch_is_exact_and_replay_does_not_post_again(worker_root):
    from mux.contracts.config import BackendConfig

    request, root, _, resolved, prices, calls, _, _, _ = worker_root

    async def preparation(actual_parent):
        assert actual_parent == root
        return request

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        deps=replace(request.deps, fleet_enabled=True),
        pool=fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
    )
    owner = FleetOrchestrator(ports)
    clone = resolved.source_agent
    terms = dict(
        now=request.now(),
        run_id=root.fleet.run_id,
        operation_key="real-installed-dispatch",
        role="worker",
        backend=BackendConfig.model_validate(resolved.backend.model_dump(mode="json")),
        agent=resolved.spec if clone is None else None,
        clone=clone[0] if clone else None,
        clone_revision=clone[1] if clone else None,
        overrides=AgentPatch.model_validate(resolved.spec.model_dump(mode="json"))
        if clone
        else None,
        limits=resolved.limits,
    )
    try:
        policy = await _policy(request)
        first = await owner.spawn(root, policy, **terms)
        # Retry before execution begins: the same committed worker, not a second allocation.
        second = await owner.spawn(root, policy, **terms)
        assert second["worker_id"] == first["worker_id"]
        async with asyncio.timeout(10):
            while True:
                _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
                actual = next(row for row in rows if str(row.id) == first["worker_id"])
                if actual.status == "succeeded":
                    break
                await asyncio.sleep(0.01)
        assert actual.ordinal == 1 and actual.result is not None
        assert rows[0].status == "starting" and rows[0].binding_id is None
        posts = [call.url.path for call in calls if call.method == "POST"]
        assert posts == [
            "/v1/agents",
            "/v1/agents/sessions",
            "/v1/agents/sessions/native-openai-session/events",
        ]
        again = await owner.spawn(root, policy, **terms)
        assert again["worker_id"] == first["worker_id"] and again["status"] == "succeeded"
        assert [call.url.path for call in calls if call.method == "POST"] == posts
    finally:
        await ports.close()
    assert request.deps.running_turns.count() == 0


async def test_shared_preparation_factory_reads_each_actual_root(
    db_preflight, db_committing_engine, root_turn
):
    from contextlib import aclosing
    from datetime import UTC, datetime

    from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
    from daimon.core.turn.runtime_manifest import read_frozen_root

    await freeze(root_turn)
    async with aclosing(root_turn_factory(db_preflight, db_committing_engine)) as second_roots:
        second = await anext(second_roots)
        await freeze(second)
        requests, parents = {}, []
        for value in (root_turn, second):
            deps, prepared, _, intent, _, scope, _, _ = value
            parent, _ = await read_frozen_root(
                deps, scope, intent_id=intent.id, now=datetime.now(UTC)
            )
            parents.append(parent)
            requests[scope] = ProviderPreparationRequest(
                deps,
                prepared.admission,
                scope,
                prepared.admission.grant.tenant_id,
                parent.fleet.root.channel.platform,
                prepared.admission.grant.subject.platform_user_id,
                parent.fleet.root.thread_id,
                prepared.session_account_id,
                False,
                DEFAULT_MA_CAPABILITIES,
                None,
                parent.limits.deadline,
                lambda: datetime.now(UTC),
            )

        async def preparation(parent):
            return requests[parent.fleet.scope]

        ports = HostFleetPorts(
            root_turn[0].sessionmaker,
            prices={},
            markup=Decimal(1),
            preparation=preparation,
            deps=root_turn[0],
        )
        captured = [await ports.capture_source(parent) for parent in parents]
        assert [source.resource_scope for source in captured] == [
            parent.fleet.scope for parent in parents
        ]
        assert captured[0].tenant_id != captured[1].tenant_id
        assert captured[0].agent_id != captured[1].agent_id


@pytest.mark.parametrize("exact_worker", [False, True])
async def test_stopped_pending_worker_is_never_claimed_and_the_single_watch_cancels(
    worker_root, exact_worker
):
    from daimon.core.channel_budget import (
        admit_fleet_worker,
        produce_fleet_budget,
        watch_fleet_budget,
    )

    request, root, _, resolved, prices, calls, _, _, _ = worker_root
    policy = await _policy(request)
    async with request.deps.sessionmaker.begin() as db:
        usage = await admit_fleet_worker(
            db,
            parent=root,
            policy=policy,
            resolved=resolved,
            ordinal=1,
            role="worker",
            prices=prices,
            markup=Decimal(1),
        )
    assert usage.admitted and usage.worker.status == "pending"
    async with request.deps.sessionmaker.begin() as db:
        await fleet.lock_budget(
            db, tenant_id=usage.run.tenant_id, actor_id=root.fleet.account_id, run_id=usage.run.id
        )
        stopped = await fleet.request_worker_stop(
            db,
            tenant_id=usage.worker.tenant_id,
            worker_id=usage.worker.id,
            expected_version=usage.worker.version,
        )
    assert stopped.status == "pending" and stopped.reserved_usd == usage.worker.reserved_usd
    pool = fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2)
    assert (
        await pool.claim(
            request.deps.sessionmaker,
            tenant_id=stopped.tenant_id,
            run_id=stopped.run_id,
            worker_id=stopped.id if exact_worker else None,
        )
        is None
    )
    async with request.deps.sessionmaker.begin() as db:
        current = (await produce_fleet_budget(db, parent=root, prices=prices, markup=Decimal(1)))[
            stopped.id
        ]
    assert not current.admitted and current.reason == "worker_stop_requested"
    cancel, finished = asyncio.Event(), asyncio.Event()
    watch = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=request.deps.sessionmaker,
            parent=root,
            prices=prices,
            markup=Decimal(1),
            cancelled={stopped.id: cancel},
            finished=finished,
        )
    )
    try:
        async with asyncio.timeout(10):
            await cancel.wait()
    finally:
        finished.set()
        await watch
    _, rows = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=stopped.tenant_id, run_id=stopped.run_id
    )
    assert next(row for row in rows if row.id == stopped.id) == stopped
    assert not calls


@pytest.mark.parametrize("dependencies", [False, True])
async def test_falsey_injected_dispatch_remains_selected_without_native_fallback(
    worker_root, dependencies
):
    request, root, _, resolved, prices, calls, _, _, _ = worker_root
    captured = await request.capture_root(root)
    dispatched, loaded = [], []

    class Dispatch:
        def __bool__(self):
            return False

        async def __call__(self, parent, row, specification, review):
            dispatched.append(row)

    async def preparation(parent):
        loaded.append(parent)
        return request

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        dispatch=Dispatch(),
        preparation=preparation if dependencies else None,
        pool=fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2)
        if dependencies
        else None,
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        terms = _spawn_terms(request, root, resolved, "falsey-dispatch")
        terms.update(agent=resolved.spec, clone=None, clone_revision=None, overrides=None)
        reply = await FleetOrchestrator(ports).spawn(
            root,
            await _policy(request),
            captured_source=captured.source,
            **terms,
        )
        assert len(dispatched) == 1 and str(dispatched[0].id) == reply["worker_id"]
        assert not loaded and not calls
    finally:
        await ports.close()


async def test_falsey_preparation_and_pool_run_the_actual_worker(worker_root):
    request, root, _, resolved, prices, calls, _, _, _ = worker_root

    class Preparation:
        def __bool__(self):
            return False

        async def __call__(self, parent):
            return request

    class Pool(fleet.WorkerPool):
        def __bool__(self):
            return False

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=Preparation(),
        pool=Pool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        reply = await FleetOrchestrator(ports).spawn(
            root,
            await _policy(request),
            **_spawn_terms(request, root, resolved, "falsey-preparation"),
        )
        row = await _settled(ports, root, reply["worker_id"])
        assert row.status == "succeeded" and row.result is not None
        assert [call.url.path for call in calls if call.method == "POST"] == [
            "/v1/agents",
            "/v1/agents/sessions",
            "/v1/agents/sessions/native-openai-session/events",
        ]
    finally:
        await ports.close()
    assert request.deps.running_turns.count() == 0


@pytest.mark.parametrize("phase", ["expired", "capture", "funded"])
async def test_loader_is_bounded_before_and_after_admission_without_native_effects(
    worker_root, phase
):
    request, root, _, resolved, prices, calls, _, _, _ = worker_root
    root = replace(
        root,
        limits=root.limits.model_copy(
            update={
                "deadline": datetime.now(UTC) + timedelta(seconds=-1 if phase == "expired" else 2),
            }
        ),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    before, rows_before = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
    )
    async with request.deps.sessionmaker() as db:
        revision_before = await mux_state.effective_revision(
            db, root.fleet.root.channel, resolved.backend.content_digest(), "agent"
        )

    async def preparation(parent):
        _, rows = await fleet.run_status(
            request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
        )
        if phase != "funded" or any(row.ordinal == 1 for row in rows):
            entered.set()
            await release.wait()
        return request

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        async with asyncio.timeout(5):
            with pytest.raises(FleetRefused, match="preparation_deadline_exceeded"):
                if phase == "funded":
                    terms = _spawn_terms(request, root, resolved, "blocked-loader")
                    terms["limits"] = resolved.limits.model_copy(
                        update={"deadline": root.limits.deadline}
                    )
                    await FleetOrchestrator(ports).spawn(
                        root,
                        await _policy(request),
                        **terms,
                    )
                else:
                    await ports.capture_source(root)
        assert entered.is_set() is (phase != "expired")
        assert request.deps.running_turns.count() == 0 and not calls
        async with request.deps.sessionmaker() as db:
            assert (
                await mux_state.effective_revision(
                    db, root.fleet.root.channel, resolved.backend.content_digest(), "agent"
                )
                == revision_before
            )
        after, rows_after = await fleet.run_status(
            request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
        )
        if phase == "funded":
            added = next(row for row in rows_after if row.ordinal == 1)
            assert added.status == "pending" and added.binding_id is None
            assert added.reserved_usd == added.budget_usd > 0
            assert after.reserved_usd > before.reserved_usd
            assert rows_after[0] == rows_before[0]
        else:
            assert after == before and rows_after == rows_before
    finally:
        release.set()
        await ports.close()


@pytest.fixture
async def paused_source(worker_root):
    request, root, _, resolved, prices, calls, _, transport, source = worker_root
    entered, release = asyncio.Event(), asyncio.Event()
    blocked = []
    transport.router.routes.clear()

    class Read(httpx.AsyncByteStream):
        async def __aiter__(self):
            _, rows = await fleet.run_status(
                request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
            )
            if not blocked and any(row.ordinal == 1 and row.status == "starting" for row in rows):
                blocked.append(asyncio.current_task())
                entered.set()
                await release.wait()
            yield json.dumps(source).encode()

    transport.router.add(
        "GET",
        re.escape("/v1/agents/" + request.admission.agent.id),
        lambda call, match: httpx.Response(200, stream=Read()),
    )

    try:
        yield worker_root, entered, release, blocked
    finally:
        release.set()


@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize("fault", ["stale", "policy", "cancel"])
async def test_dispatcher_ends_only_its_known_pre_native_claim_and_allows_a_healthy_run(
    paused_source, fault, stop
):
    worker_root, entered, release, blocked = paused_source
    request, root, _, resolved, prices, calls, _, _, source = worker_root
    original_policy, original_version = await _policy(request), source["version"]

    async def preparation(parent):
        return request

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
        deps=replace(request.deps, fleet_enabled=True),
    )
    owner = FleetOrchestrator(ports)
    try:
        terms = _spawn_terms(request, root, resolved, "pre-native-interleave")
        reply = await owner.spawn(root, original_policy, **terms)
        async with asyncio.timeout(10):
            await entered.wait()
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        claimed = next(row for row in rows if str(row.id) == reply["worker_id"])
        assert claimed.status == "starting" and claimed.reserved_usd == claimed.budget_usd > 0
        wake_key = await _arm_completion(request, root, claimed)
        if stop:
            async with request.deps.sessionmaker.begin() as db:
                await fleet.lock_budget(
                    db,
                    tenant_id=claimed.tenant_id,
                    actor_id=root.fleet.account_id,
                    run_id=claimed.run_id,
                )
                stopped = await fleet.request_worker_stop(
                    db,
                    tenant_id=claimed.tenant_id,
                    worker_id=claimed.id,
                    expected_version=claimed.version,
                )
            assert stopped.status == "starting" and stopped.error_code == "stop_requested"
            assert stopped.version == claimed.version + 1
        if fault == "stale":
            source["version"] += 1
        elif fault == "policy":
            async with request.deps.sessionmaker.begin() as db:
                await access_policy.set_access_policy(
                    db,
                    tenant_id=request.tenant_id,
                    policy=original_policy.model_copy(update={"enabled_backends": ("anthropic",)}),
                )
        else:
            blocked[0].cancel()
        release.set()
        ended = await _settled(ports, root, reply["worker_id"])
        # A person's Stop reads as stopped, never as a refusal; a cancel from outside
        # is never a refusal either (#1413 review B3).
        expected = (
            ("cancelled", "stop_requested")
            if stop
            else ("failed", "watch_ended")
            if fault == "cancel"
            else ("failed", "preparation_refused")
        )
        assert (ended.status, ended.error_code) == expected
        assert ended.binding_id is None and ended.attempts == ended.turns_used == 0
        assert ended.reserved_usd == claimed.reserved_usd and not calls
        await _assert_completion_offered(request, wake_key)
        source["version"] = original_version
        async with request.deps.sessionmaker.begin() as db:
            await access_policy.set_access_policy(
                db, tenant_id=request.tenant_id, policy=original_policy
            )
        replay = await owner.spawn(root, original_policy, **terms)
        assert replay["worker_id"] == reply["worker_id"] and replay["status"] == ended.status
        assert not calls
        first, _ = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        healthy = await fleet.create_run(
            request.deps.sessionmaker,
            tenant_id=first.tenant_id,
            orchestrator_account_id=first.orchestrator_account_id,
            orchestrator_session_id=first.orchestrator_session_id,
            orchestrator_binding_id=first.orchestrator_binding_id,
            platform=first.platform,
            channel_id=first.channel_id,
            thread_id=first.thread_id,
            operation_key="healthy-after-refusal",
            total_budget_usd=first.total_budget_usd,
            deadline_at=first.deadline_at,
            max_workers=2,
            created_by="host",
        )
        healthy_root = replace(root, fleet=replace(root.fleet, run_id=healthy.id))
        next_reply = await owner.spawn(
            healthy_root,
            original_policy,
            **_spawn_terms(request, healthy_root, resolved, "healthy-after-refusal"),
        )
        succeeded = await _settled(ports, healthy_root, next_reply["worker_id"])
        assert succeeded.status == "succeeded" and succeeded.result is not None
        assert [call.url.path for call in calls if call.method == "POST"] == [
            "/v1/agents",
            "/v1/agents/sessions",
            "/v1/agents/sessions/native-openai-session/events",
        ]
    finally:
        source["version"] = original_version
        release.set()
        await ports.close()
    assert request.deps.running_turns.count() == 0


@pytest.mark.parametrize("phase", ["requeued", "reclaimed", "terminal"])
async def test_pre_native_cleanup_refuses_a_changed_claim_without_overwriting_it(
    paused_source, phase
):
    worker_root, entered, release, blocked = paused_source
    request, root, _, resolved, prices, calls, _, _, source = worker_root
    original_version = source["version"]

    async def preparation(parent):
        return request

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
        deps=replace(request.deps, fleet_enabled=True),
    )
    owner = FleetOrchestrator(ports)
    policy = await _policy(request)
    terms = _spawn_terms(request, root, resolved, "changed-pre-native-claim")
    try:
        reply = await owner.spawn(root, policy, **terms)
        async with asyncio.timeout(10):
            await entered.wait()
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        claimed = next(row for row in rows if str(row.id) == reply["worker_id"])
        async with request.deps.sessionmaker.begin() as db:
            await fleet.lock_budget(
                db,
                tenant_id=claimed.tenant_id,
                actor_id=root.fleet.account_id,
                run_id=claimed.run_id,
            )
            if phase == "terminal":
                changed = await fleet.finish(
                    db,
                    tenant_id=claimed.tenant_id,
                    worker_id=claimed.id,
                    expected_version=claimed.version,
                    status="cancelled",
                    error_code="stop_requested",
                )
            else:
                changed = await fleet.requeue(
                    db,
                    tenant_id=claimed.tenant_id,
                    worker=claimed,
                    max_attempts=3,
                )
                if phase == "reclaimed":
                    changed = await fleet.claim_worker(
                        db,
                        tenant_id=claimed.tenant_id,
                        run_id=claimed.run_id,
                        worker_id=claimed.id,
                        cap=2,
                    )
                    assert changed is not None and changed.status == "starting"
                assert changed.attempts == claimed.attempts + 1
        source["version"] += 1
        release.set()
        async with asyncio.timeout(10):
            with pytest.raises((WorkerSpecRefused, AdmissionDenied)):
                await blocked[0]
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        retained = next(row for row in rows if row.id == claimed.id)
        assert retained == changed
        assert retained.reserved_usd == claimed.reserved_usd > 0 and not calls
    finally:
        source["version"] = original_version
        release.set()
        await ports.close()
    assert request.deps.running_turns.count() == 0


async def _arm_completion(request, root, worker):
    from daimon.core.continuity.continuation import ContinuationRequest
    from daimon.core.continuity.wakes import arm_worker_finished, worker_finished_key

    run, _ = await fleet.run_status(
        request.deps.sessionmaker,
        tenant_id=worker.tenant_id,
        run_id=worker.run_id,
    )
    key = worker_finished_key(run.id, (worker.id,))
    continuation = ContinuationRequest(
        tenant_id=run.tenant_id,
        platform=run.platform,
        parent_channel_id=run.channel_id,
        thread_id=run.thread_id,
        requester_account_id=run.orchestrator_account_id,
        requester_external_user_id=request.external_user_id,
        target_ma_agent_id=request.admission.agent.id,
        target_name=request.admission.config.agent_name,
        requested_work=None,
        reason="worker_finished",
        idempotency_key=key,
    )
    async with request.deps.sessionmaker.begin() as db:
        await fleet.lock_budget(
            db,
            tenant_id=run.tenant_id,
            actor_id=root.fleet.account_id,
            run_id=run.id,
        )
        await arm_worker_finished(
            db,
            continuation,
            run_id=run.id,
            worker_ids=(worker.id,),
            now=request.now(),
        )
    return key


async def _assert_completion_offered(request, key):
    from daimon.core.stores.task_continuations import get_continuation

    async with request.deps.sessionmaker() as db:
        wake = await get_continuation(db, idempotency_key=key)
    assert wake is not None and wake.status == "pending" and wake.available_at is not None
    assert wake.available_at <= datetime.now(UTC)


@pytest.mark.parametrize("fault", ["policy", "stop"])
async def test_run_worker_prelude_refusal_ends_claim_and_publishes_completion(
    worker_root, monkeypatch, fault
):
    from daimon.core import headless_runner

    request, root, _, resolved, prices, calls, _, _, _ = worker_root
    entered, release = asyncio.Event(), asyncio.Event()
    original_policy = await _policy(request)
    real_runner, real_load = headless_runner.run_worker, access_policy.load_access_policy
    runner_tasks = []

    async def runner(prepared, **kwargs):
        runner_tasks.append(asyncio.current_task())
        return await real_runner(prepared, **kwargs)

    async def load(db, **kwargs):
        if runner_tasks and asyncio.current_task() is runner_tasks[0] and not entered.is_set():
            entered.set()
            await release.wait()
        return await real_load(db, **kwargs)

    monkeypatch.setattr(headless_runner, "run_worker", runner)
    monkeypatch.setattr(headless_runner.policy_store, "load_access_policy", load)

    async def preparation(parent):
        return request

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
        deps=replace(request.deps, fleet_enabled=True),
    )
    owner = FleetOrchestrator(ports)
    try:
        terms = _spawn_terms(request, root, resolved, "worker-prelude-interleave")
        reply = await owner.spawn(root, original_policy, **terms)
        async with asyncio.timeout(10):
            await entered.wait()
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        claimed = next(row for row in rows if str(row.id) == reply["worker_id"])
        wake_key = await _arm_completion(request, root, claimed)
        async with request.deps.sessionmaker.begin() as db:
            if fault == "policy":
                await access_policy.set_access_policy(
                    db,
                    tenant_id=request.tenant_id,
                    policy=original_policy.model_copy(update={"enabled_backends": ("anthropic",)}),
                )
            else:
                await fleet.lock_budget(
                    db,
                    tenant_id=claimed.tenant_id,
                    actor_id=root.fleet.account_id,
                    run_id=claimed.run_id,
                )
                stopped = await fleet.request_worker_stop(
                    db,
                    tenant_id=claimed.tenant_id,
                    worker_id=claimed.id,
                    expected_version=claimed.version,
                )
                assert stopped.version == claimed.version + 1
        if fault == "stop":
            # Observe the actual budget watch; do not cancel the task directly.
            async with asyncio.timeout(10):
                while not ports._watches[claimed.run_id][0][claimed.id].is_set():
                    await asyncio.sleep(0.01)
        release.set()
        ended = await _settled(ports, root, reply["worker_id"])
        # A Stop ends the worker as stopped, never as a refusal (#1413 review B3).
        assert (ended.status, ended.error_code) == (
            ("cancelled", "stop_requested")
            if fault == "stop"
            else ("failed", "preparation_refused")
        )
        assert ended.binding_id is None and ended.attempts == ended.turns_used == 0
        assert ended.reserved_usd == claimed.reserved_usd > 0 and not calls
        await _assert_completion_offered(request, wake_key)
        live_run, _ = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        assert live_run.status == "running"
        async with request.deps.sessionmaker.begin() as db:
            await access_policy.set_access_policy(
                db, tenant_id=request.tenant_id, policy=original_policy
            )
        replay = await owner.spawn(root, original_policy, **terms)
        assert replay["worker_id"] == reply["worker_id"] and replay["status"] == ended.status
        assert not calls
        first, _ = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        healthy = await fleet.create_run(
            request.deps.sessionmaker,
            tenant_id=first.tenant_id,
            orchestrator_account_id=first.orchestrator_account_id,
            orchestrator_session_id=first.orchestrator_session_id,
            orchestrator_binding_id=first.orchestrator_binding_id,
            platform=first.platform,
            channel_id=first.channel_id,
            thread_id=first.thread_id,
            operation_key="healthy-after-prelude-refusal",
            total_budget_usd=first.total_budget_usd,
            deadline_at=first.deadline_at,
            max_workers=2,
            created_by="host",
        )
        healthy_root = replace(root, fleet=replace(root.fleet, run_id=healthy.id))
        next_reply = await owner.spawn(
            healthy_root,
            original_policy,
            **_spawn_terms(request, healthy_root, resolved, "healthy-after-prelude-refusal"),
        )
        succeeded = await _settled(ports, healthy_root, next_reply["worker_id"])
        assert succeeded.status == "succeeded" and succeeded.result is not None
        assert len([call for call in calls if call.method == "POST"]) == 3
    finally:
        release.set()
        await ports.close()
    assert request.deps.running_turns.count() == 0


async def test_wedged_preparation_fails_promptly_and_releases_its_claim(paused_source, monkeypatch):
    from daimon.core import fleet_orchestration

    worker_root, entered, _, _ = paused_source
    request, root, _, resolved, prices, calls, _, _, _ = worker_root
    monkeypatch.setattr(fleet_orchestration, "WORKER_PREPARATION_TIMEOUT_S", 0.5)

    async def preparation(parent):
        return request

    pool = fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=2)
    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=pool,
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        reply = await FleetOrchestrator(ports).spawn(
            root,
            await _policy(request),
            **_spawn_terms(request, root, resolved, "wedged-preparation"),
        )
        async with asyncio.timeout(5):
            await entered.wait()
            ended = await _settled(ports, root, reply["worker_id"])
        assert ended.status == "failed" and ended.error_code == "preparation_deadline_exceeded"
        assert ended.binding_id is None and ended.turns_used == 0 and not calls
        # A second real store claim must succeed through the same size-one pool.
        _, rows = await fleet.run_status(
            request.deps.sessionmaker, tenant_id=ended.tenant_id, run_id=ended.run_id
        )
        seeded = next(row for row in rows if row.ordinal == 0)
        assert seeded.status == "starting" and seeded.turns_used == 0 and seeded.binding_id is None
        pending = await fleet.requeue(
            request.deps.sessionmaker, tenant_id=seeded.tenant_id, worker=seeded, max_attempts=2
        )
        claimed = await pool.claim(
            request.deps.sessionmaker,
            tenant_id=pending.tenant_id,
            run_id=pending.run_id,
            worker_id=pending.id,
        )
        assert claimed is not None
        pool.release()
    finally:
        await ports.close()


@pytest.mark.parametrize("kind", ["capability", "admission"])
async def test_worker_failure_log_retains_closed_refusal_without_private_payloads(
    worker_root, monkeypatch, kind
):
    import json

    from daimon.core.turn.prepare import ProviderPreparationRequest
    from mux.errors import UnsupportedCapability
    from structlog.testing import capture_logs

    request, root, _, resolved, prices, calls, _, _, _ = worker_root
    private = "PRIVATE_PROVIDER_BODY_KEY_SQL"
    error = (
        UnsupportedCapability(("native_worker_resource_drop", private), private)
        if kind == "capability"
        else AdmissionDenied(reason="backend_unsupported")
    )
    error.__cause__ = RuntimeError(private)

    async def refuse(self, parent, worker):
        raise error

    async def preparation(parent):
        return request

    monkeypatch.setattr(ProviderPreparationRequest, "for_worker", refuse)
    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        deps=replace(request.deps, fleet_enabled=True),
        pool=fleet.WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
    )
    try:
        with capture_logs() as logs:
            result = await FleetOrchestrator(ports).spawn(
                root, await _policy(request), **_spawn_terms(request, root, resolved, "refused-log")
            )
            async with asyncio.timeout(10):
                while not any(row.get("event") == "fleet.worker_task_failed" for row in logs):
                    await asyncio.sleep(0.01)
        row = next(row for row in logs if row["event"] == "fleet.worker_task_failed")
        expected = (
            {
                "type": "UnsupportedCapability",
                "missing": ["native_worker_resource_drop", "redacted"],
            }
            if kind == "capability"
            else {"type": "AdmissionDenied", "reason": "backend_unsupported"}
        )
        assert row["causes"] == [expected, {"type": "RuntimeError"}]
        assert row["run_id"] == str(root.fleet.run_id)
        assert row["worker_id"] == result["worker_id"]
        assert row["error_class"] == type(error).__name__
        assert private not in json.dumps(row)
        assert not calls  # Refusal logging never creates/sends a native request.
    finally:
        await ports.close()


@pytest.mark.parametrize("error_kind", ["fleet", "unexpected", "already_terminal"])
async def test_dispatch_failure_ends_claim_and_preserves_siblings_and_collection(
    worker_root, monkeypatch, error_kind
):
    from daimon.core import headless_runner
    from daimon.core.fleet import FleetConflict
    from structlog.testing import capture_logs

    request, root, sibling, resolved, prices, calls, _, _, _ = worker_root
    private = "PRIVATE_PROVIDER_BODY_KEY_SQL"
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(prepared, *, worker, **kwargs):
        entered.set()
        await release.wait()
        if error_kind == "already_terminal":
            async with request.deps.sessionmaker.begin() as db:
                await fleet.lock_budget(
                    db,
                    tenant_id=worker.tenant_id,
                    actor_id=root.fleet.account_id,
                    run_id=worker.run_id,
                )
                await fleet.finish(
                    db,
                    tenant_id=worker.tenant_id,
                    worker_id=worker.id,
                    expected_version=worker.version,
                    status="failed",
                    error_code="deadline_expired",
                )
        if error_kind == "unexpected":
            raise ValueError(private)
        error = FleetConflict(private, reason="journal_owner_mismatch")
        error.__cause__ = RuntimeError(private)
        raise error

    async def preparation(parent):
        return request

    monkeypatch.setattr(headless_runner, "run_worker", fail)
    pool = fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=2)
    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=pool,
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        with capture_logs() as logs:
            reply = await FleetOrchestrator(ports).spawn(
                root,
                await _policy(request),
                **_spawn_terms(request, root, resolved, "dispatch-failure"),
            )
            async with asyncio.timeout(5):
                await entered.wait()
            _, claimed_rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
            claimed = next(row for row in claimed_rows if str(row.id) == reply["worker_id"])
            wake_key = await _arm_completion(request, root, claimed)
            release.set()
            ended = await _settled(ports, root, reply["worker_id"])
            async with asyncio.timeout(5):
                while not any(row.get("event") == "fleet.worker_task_failed" for row in logs):
                    await asyncio.sleep(0.01)
        assert ended.status == "failed"
        # An unexpected exception is a bug on our side, never "couldn't start on that model".
        assert ended.error_code == {
            "already_terminal": "deadline_expired",
            "unexpected": "setup_error",
        }.get(error_kind, "dispatch_refused")
        run, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        assert run.status == "running"
        assert next(row for row in rows if row.id == sibling.id) == sibling
        await _assert_completion_offered(request, wake_key)
        assert ended.binding_id is None and ended.turns_used == 0 and not calls
        # Terminalization never releases money on an unmeasured failure.
        assert ended.reserved_usd > 0 and run.reserved_usd >= ended.reserved_usd
        row = next(row for row in logs if row.get("event") == "fleet.worker_task_failed")
        expected = (
            [{"type": "ValueError"}]
            if error_kind == "unexpected"
            else [
                {"type": "FleetConflict", "reason": "journal_owner_mismatch"},
                {"type": "RuntimeError"},
            ]
        )
        assert row["causes"] == expected and private not in json.dumps(row)
        from daimon.core.observability import redact_log_event

        scrubbed = redact_log_event(None, "warning", row)
        assert scrubbed["causes"] == expected and private not in json.dumps(scrubbed)
    finally:
        release.set()
        await ports.close()


def test_fleet_conflict_unknown_reason_is_redacted():
    from daimon.core.channel_backend import backend_refusal_causes
    from daimon.core.fleet import FleetConflict

    error = FleetConflict("PRIVATE_PROVIDER_BODY_KEY_SQL")
    error.reason = "PRIVATE_PROVIDER_BODY_KEY_SQL"  # pyright: ignore[reportAttributeAccessIssue]
    assert backend_refusal_causes(error) == [{"type": "FleetConflict", "reason": "redacted"}]


async def test_dispatch_failure_cannot_terminalize_a_requeued_claim(worker_root):
    from daimon.core.fleet import FleetConflict
    from daimon.core.headless_runner import commit_worker_transition

    request, root, claimed, _, _, _, _, _, _ = worker_root
    pending = await fleet.requeue(
        request.deps.sessionmaker, tenant_id=claimed.tenant_id, worker=claimed, max_attempts=2
    )
    pool = fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=2)
    newer = await pool.claim(
        request.deps.sessionmaker,
        tenant_id=pending.tenant_id,
        run_id=pending.run_id,
        worker_id=pending.id,
    )
    assert newer is not None and newer.attempts == claimed.attempts + 1
    try:
        with pytest.raises(FleetConflict) as exc:
            await commit_worker_transition(request, root, claimed, failure_code="dispatch_refused")
        assert exc.value.reason == "claim_changed"
        run, rows = await fleet.run_status(
            request.deps.sessionmaker, tenant_id=claimed.tenant_id, run_id=claimed.run_id
        )
        assert run.status == "running"
        assert next(row for row in rows if row.id == newer.id) == newer
    finally:
        pool.release()


async def test_fleet_r7_retry_reuses_defaulted_limits_instead_of_conflicting(worker_root):
    """#1298 review: a bounded default deadline (now + 120 s) moves on retry.

    The retry must reuse the admitted worker's limits; recomputing them would change
    the admission digest and refuse the replay as an operation conflict.
    """
    from datetime import timedelta

    request, root, _, resolved, prices, calls, _, _, _ = worker_root
    captured = await request.capture_root(root)
    dispatched = []

    class Dispatch:
        async def __call__(self, parent, row, specification, review):
            dispatched.append(row)

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        dispatch=Dispatch(),
        deps=replace(request.deps, fleet_enabled=True),
    )
    owner = FleetOrchestrator(ports)
    try:
        policy = await _policy(request)
        replies = []
        start = request.now()
        for later in (0, 5):
            now = start + timedelta(seconds=later)
            limits = await owner.spawn_limits(
                root,
                policy,
                operation_key="bounded-default",
                provider=resolved.spec.model.provider,
                now=now,
                budget_usd=resolved.limits.budget_usd,
                max_turns=resolved.limits.max_turns,
                max_runtime_s=30,
            )
            if later == 0:
                first = limits
                # Shorter than this run's deadline, so a fresh default moves with time.
                assert limits.deadline == start + timedelta(seconds=30)
                assert limits.deadline < root.limits.deadline
            else:
                # A fresh default would be 5 s later and change the admission digest.
                assert limits.deadline == first.deadline
            terms = _spawn_terms(request, root, resolved, "bounded-default")
            terms.update(
                now=now,
                limits=limits,
                agent=resolved.spec,
                clone=None,
                clone_revision=None,
                overrides=None,
            )
            replies.append(
                await owner.spawn(root, policy, captured_source=captured.source, **terms)
            )
        assert replies[0]["worker_id"] == replies[1]["worker_id"]
        # A replay of a still-pending worker re-runs its idempotent dispatch only.
        assert {str(row.id) for row in dispatched} == {replies[0]["worker_id"]} and not calls
        _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
        assert sum(1 for row in rows if str(row.id) == replies[0]["worker_id"]) == 1
    finally:
        await ports.close()


async def test_a_bug_while_running_a_worker_is_recorded_as_a_setup_error(worker_root, monkeypatch):
    """RESCORE-2 J4: a ValidationError read on the card as the model being unable to run."""
    from daimon.core import headless_runner
    from mux.contracts.resources import AgentPatch

    request, root, _, resolved, prices, calls, _, _, _ = worker_root

    async def preparation(parent):
        return request

    async def broken(*args, **kwargs):
        AgentPatch.model_validate({"extensions": [{"namespace": "x", "version": 1}]})
        raise AssertionError("expected a ValidationError")

    monkeypatch.setattr(headless_runner, "run_worker", broken)
    pool = fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=2)
    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=pool,
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        reply = await FleetOrchestrator(ports).spawn(
            root, await _policy(request), **_spawn_terms(request, root, resolved, "bug-in-run")
        )
        async with asyncio.timeout(10):
            ended = await _settled(ports, root, reply["worker_id"])
        assert ended.status == "failed" and ended.error_code == "setup_error"
        assert not calls
    finally:
        await ports.close()


@pytest.mark.parametrize("recorded", [True, False])
async def test_dispatch_skips_a_second_failure_commit_run_worker_already_recorded(
    worker_root, monkeypatch, recorded
):
    """#1416: run_worker records its own pre-native failure; a second commit from the
    dispatcher would only wait on the tenant fleet lock again."""
    from daimon.core import headless_runner
    from structlog.testing import capture_logs

    request, root, _, resolved, prices, _, _, _, _ = worker_root
    commits: list[str] = []

    async def fail(*args, **kwargs):
        error = RuntimeError("worker failed")
        if recorded:
            setattr(error, headless_runner.FAILURE_RECORDED, True)
        raise error

    async def counted(*args, failure_code, **kwargs):
        commits.append(failure_code)

    async def preparation(parent):
        return request

    monkeypatch.setattr(headless_runner, "run_worker", fail)
    monkeypatch.setattr(headless_runner, "commit_worker_failure", counted)
    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=2),
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        with capture_logs() as logs:
            await FleetOrchestrator(ports).spawn(
                root,
                await _policy(request),
                **_spawn_terms(request, root, resolved, f"recorded-{recorded}"),
            )
            async with asyncio.timeout(10):
                while not any(row.get("event") == "fleet.worker_task_failed" for row in logs):
                    await asyncio.sleep(0.01)
        assert commits == ([] if recorded else ["setup_error"])
    finally:
        await ports.close()


async def test_a_dispatched_turn_at_the_ceiling_is_recorded_as_a_timeout_end_to_end(
    worker_root, monkeypatch
):
    """LEAD (#1413 approval): through execute(), not just worker_failure_code."""
    from daimon.core import headless_runner
    from daimon.core.turn.ceiling import ceiling_error
    from daimon.core.turn.run import RunOutcome
    from daimon.core.turn.state import TurnState
    from daimon.core.turn.termination import TerminationReason

    request, root, _, resolved, prices, _, _, _, _ = worker_root

    async def ceiling(*_args, **_kwargs):
        return RunOutcome(
            state=TurnState(error=ceiling_error(), termination=TerminationReason.CEILING),
            ma_session_id="session",
            mapping_id=None,
            recovered=False,
        )

    monkeypatch.setattr(headless_runner, "run_prepared_turn", ceiling)

    async def preparation(parent):
        return request

    ports = HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=2),
        deps=replace(request.deps, fleet_enabled=True),
    )
    try:
        reply = await FleetOrchestrator(ports).spawn(
            root,
            await _policy(request),
            **_spawn_terms(request, root, resolved, "dispatched-ceiling"),
        )
        ended = await _settled(ports, root, reply["worker_id"])
        expected = "provider_busy" if ended.backend == "gemini" else "timed_out"
        assert (ended.status, ended.error_code) == ("failed", expected)
    finally:
        await ports.close()
