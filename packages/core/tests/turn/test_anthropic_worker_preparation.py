"""Fresh native workers through owned receipt preparation and the real driver."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from anthropic import APIConnectionError
from daimon.core.stores import fleet, mux_state

from ..test_fleet_budget import tariffs
from .test_native_session_receipts import native_worker as native_worker
from .test_native_session_receipts import source_read_only as source_read_only

NOW = datetime(2026, 10, 12, 18, tzinfo=UTC)


@pytest.mark.parametrize("source_read_only", [True, False], indirect=True)
async def test_fresh_native_worker_runs_actual_funded_headless_caller(native_worker):
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    request, root, claimed, trace, _ = native_worker
    result = await run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Complete the task",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded" and result.turns_used == 1
    assert result.binding_id is not None and result.result.excerpt == "verified-worker-result"
    assert sum(call.method == "POST" and call.path == "/v1/agents" for call in trace.requests) == 1
    assert (
        sum(call.method == "POST" and call.path == "/v1/sessions" for call in trace.requests) == 1
    )
    body = next(call.json() for call in trace.requests if call.path == "/v1/sessions")
    assert body["agent"] == "new-worker" and len(body["resources"]) == 1
    assert body["resources"][0]["access"] == "read_only"
    assert any(call.path.endswith("/agents/original-source") for call in trace.requests)
    await drain_outcomes()
    async with request.deps.sessionmaker() as db:
        outcomes = await list_for_tenant(db, request.tenant_id)
    assert any(row.origin == "worker" and row.reason.value == "completed" for row in outcomes)
    trace.assert_consumed()


@pytest.mark.parametrize(
    "fault", ["agent_drift", "agent_archived", "resource_drift", "source_drift", "lost_ack"]
)
async def test_native_worker_unproved_output_never_binds_or_sends(native_worker, fault):
    from daimon.core.headless_runner import run_worker

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = fault
    from daimon.core.turn.errors import AdmissionDenied
    from mux.errors import ProviderError, ScopeViolation

    with pytest.raises((AdmissionDenied, ScopeViolation, ProviderError)):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    assert sum(call.path == "/v1/agents" and call.method == "POST" for call in trace.requests) == 1
    assert sum(
        call.path == "/v1/sessions" and call.method == "POST" for call in trace.requests
    ) == (1 if fault in {"resource_drift", "source_drift"} else 0)
    assert not any(call.path.endswith("/events") for call in trace.requests)
    # A refused allocation never leaves its native agent behind (SMOKE-1: one orphan per try).
    if fault in {"agent_drift", "resource_drift", "source_drift"}:
        assert (
            sum(
                call.method == "POST" and call.path == "/v1/agents/new-worker/archive"
                for call in trace.requests
            )
            == 1
        )
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=request.tenant_id, actor_id=root.fleet.account_id, run_id=claimed.run_id
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == claimed.id)
    assert persisted.binding_id is None and persisted.turns_used == 0 and persisted.result is None


@pytest.mark.parametrize("fault", ["scope", "store", "disabled"])
async def test_native_worker_refuses_before_source_read_or_native_allocation(
    native_worker, monkeypatch, fault
):
    from daimon.core.fleet import spawn_operation_key, worker_slot
    from daimon.core.turn import runtime_manifest
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.persistence import TurnPersistence
    from daimon.core.turn.prepare import bind_session_impl
    from mux.errors import ScopeViolation

    request, root, claimed, trace, _ = native_worker
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=request.tenant_id, actor_id=root.fleet.account_id, run_id=claimed.run_id
        )
        run = graph[0][0]
    persistence = TurnPersistence(
        request.deps.state_store,
        None,
        request.scope.model_copy(update={"authorization_id": "another-grant"})
        if fault == "scope"
        else request.scope,
        slot=worker_slot(run, claimed),
        operation_key=spawn_operation_key(run.id, 0),
    )
    deps = (
        replace(request.deps, state_store=mux_state.PostgresStateStore(request.deps.sessionmaker))
        if fault == "store"
        else request.deps
    )
    if fault == "disabled":
        deps = replace(deps, agent_backends=False)

    async def unexpected(*args, **kwargs):
        pytest.fail("source or guard read preceded ownership/flag admission")

    monkeypatch.setattr(runtime_manifest, "capture_preparation_source", unexpected)
    with persistence.activate(), pytest.raises((AdmissionDenied, ScopeViolation)):
        await persistence.run(
            lambda: bind_session_impl(
                deps,
                request.admission,
                tenant_id=request.tenant_id,
                platform=request.platform,
                external_user_id=request.external_user_id,
                thread_id=worker_slot(run, claimed).thread.thread_id,
                session_account_id=request.session_account_id,
                reuse_existing=True,
                deadline=request.deadline,
                now=request.now,
                worker=request.worker,
                before_native=unexpected,
            )
        )
    assert trace.requests == []
    assert (
        await request.deps.state_store.get_operation(
            request.scope, spawn_operation_key(run.id, 0) + ":agent_create:0"
        )
        is None
    )


async def test_lost_native_agent_ack_keeps_one_claim_and_never_recreates(native_worker):
    from daimon.core.fleet import spawn_operation_key
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn.persistence import UncertainSend
    from mux.errors import ProviderError

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = "lost_ack"
    for _ in range(2):
        with pytest.raises((UncertainSend, ProviderError)):
            await run_worker(
                request,
                parent=root,
                worker=claimed,
                trigger_message="Complete the task",
                prices=tariffs(root),
                cancel=asyncio.Event(),
            )
    assert sum(call.path == "/v1/agents" and call.method == "POST" for call in trace.requests) == 1
    assert not any(
        call.path == "/v1/sessions" or call.path.endswith("/events") for call in trace.requests
    )
    claim = await request.deps.state_store.get_operation(
        request.scope, spawn_operation_key(claimed.run_id, 0) + ":agent_create:0"
    )
    assert claim is not None and claim.operation.status == "outcome_unknown"
    allocated = next(
        call.json()
        for call in trace.requests
        if call.method == "POST" and call.path == "/v1/agents"
    )
    assert allocated["metadata"]["daimon_name"] == f"fleet-{claimed.run_id}-{claimed.ordinal}"


async def test_missing_explicit_root_memory_grant_refuses_before_native(native_worker):
    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import WorkerSpecRefused

    request, root, claimed, trace, _ = native_worker
    with pytest.raises(WorkerSpecRefused) as refusal:
        await run_worker(
            request,
            parent=replace(root, resources=()),
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    assert refusal.value.field == "files" and refusal.value.reason == "not_subset"
    assert trace.requests == []


@pytest.mark.parametrize(
    "fault",
    [
        "frozen_model",
        "frozen_system",
        "frozen_version",
        "frozen_tools",
        "frozen_mcp",
        "frozen_skills",
        "frozen_get_only",
    ],
)
async def test_frozen_session_execution_must_match_the_proved_worker_before_input(
    native_worker, fault
):
    from daimon.core.headless_runner import run_worker
    from mux.errors import ScopeViolation

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = fault
    with pytest.raises(ScopeViolation):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    assert not any(call.path.endswith("/events") for call in trace.requests)
    archived = [call.path for call in trace.requests if call.path.endswith("/archive")]
    # The refused session and the agent allocated for it are both archived.
    assert sorted(archived) == [
        "/v1/agents/new-worker/archive",
        "/v1/sessions/worker-session/archive",
    ]
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=request.tenant_id, actor_id=root.fleet.account_id, run_id=claimed.run_id
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == claimed.id)
    assert persisted.binding_id is None and persisted.turns_used == 0 and persisted.result is None


async def test_lost_native_session_ack_keeps_one_claim_and_never_recreates(native_worker):
    from daimon.core.fleet import spawn_operation_key
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn.persistence import UncertainSend
    from mux.errors import ProviderError

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = "lost_session_ack"
    for _ in range(2):
        with pytest.raises((UncertainSend, ProviderError, APIConnectionError)):
            await run_worker(
                request,
                parent=root,
                worker=claimed,
                trigger_message="Complete the task",
                prices=tariffs(root),
                cancel=asyncio.Event(),
            )
    assert (
        sum(call.method == "POST" and call.path == "/v1/sessions" for call in trace.requests) == 1
    )
    assert not any(call.path.endswith("/events") for call in trace.requests)
    claim = await request.deps.state_store.get_operation(
        request.scope, spawn_operation_key(claimed.run_id, 0) + ":session_create:0"
    )
    assert claim is not None and claim.operation.status == "outcome_unknown"
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=request.tenant_id, actor_id=root.fleet.account_id, run_id=claimed.run_id
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == claimed.id)
    assert persisted.binding_id is None and persisted.turns_used == 0 and persisted.result is None


@pytest.mark.parametrize("replacement", [None, "memory-rebound"])
async def test_warm_binding_changed_after_capture_never_provisions_or_sends(
    native_worker, monkeypatch, replacement
):
    from daimon.core import sessions
    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import WorkerSpecRefused
    from daimon.core.stores.agent_memory_stores import (
        clear_memory_store,
        get_memory_store_id,
        insert_memory_store,
    )
    from mux.errors import ScopeViolation

    request, root, claimed, trace, _ = native_worker
    original = sessions.ensure_memory_store_and_mount
    changed = []

    async def change_binding(*args, **kwargs):
        async with request.deps.sessionmaker.begin() as db:
            await clear_memory_store(db, tenant_id=kwargs["tenant_id"], agent_id=kwargs["agent_id"])
            if replacement is not None:
                await insert_memory_store(
                    db,
                    tenant_id=kwargs["tenant_id"],
                    agent_id=kwargs["agent_id"],
                    memory_store_id=replacement,
                )
        changed.append((kwargs["agent_id"], len(trace.requests)))
        return await original(*args, **kwargs)

    monkeypatch.setattr(sessions, "ensure_memory_store_and_mount", change_binding)
    with pytest.raises((ScopeViolation, WorkerSpecRefused)):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    assert len(changed) == 1
    # Nothing is provisioned or sent after the change; only our own allocated agent is
    # archived, so a refused attempt leaves no orphan.
    after = [(call.method, call.path) for call in trace.requests[changed[0][1] :]]
    assert after == [("POST", "/v1/agents/new-worker/archive")]
    assert not any(
        call.method == "POST"
        and call.path in {"/v1/memory_stores", "/v1/sessions"}
        or call.path.endswith("/events")
        for call in trace.requests
    )
    async with request.deps.sessionmaker() as db:
        assert (
            await get_memory_store_id(db, tenant_id=request.tenant_id, agent_id=changed[0][0])
            == replacement
        )
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=request.tenant_id, actor_id=root.fleet.account_id, run_id=claimed.run_id
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == claimed.id)
    assert persisted.binding_id is None and persisted.turns_used == 0 and persisted.result is None


async def test_owned_session_ack_restores_after_interrupted_proof_without_allocating(
    native_worker,
):
    from daimon.core.fleet import spawn_operation_key
    from daimon.core.headless_runner import run_worker

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = "interrupted_proof"
    with pytest.raises(asyncio.CancelledError):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    receipt = await request.deps.state_store.get_operation(
        request.scope, spawn_operation_key(claimed.run_id, 0) + ":session_create:0"
    )
    assert receipt is not None and receipt.operation.status == "processed"
    assert receipt.result["id"] == "worker-session"
    assert not any(call.path.endswith("/events") for call in trace.requests)
    result = await run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Complete the task",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded" and result.turns_used == 1
    assert (
        sum(call.method == "POST" and call.path == "/v1/sessions" for call in trace.requests) == 1
    )
    assert sum(call.method == "POST" and call.path == "/v1/agents" for call in trace.requests) == 1


async def test_foreign_unproved_session_is_never_archived(native_worker):
    from daimon.core.headless_runner import run_worker
    from mux.errors import ScopeViolation

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = "foreign_session"
    with pytest.raises(ScopeViolation):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    # The foreign session is never touched; the agent we allocated and verified is ours.
    assert not any(
        call.path.endswith("/events")
        or call.path.startswith("/v1/sessions/")
        and "archive" in call.path
        for call in trace.requests
    )
    assert [call.path for call in trace.requests if call.path.endswith("/archive")] == [
        "/v1/agents/new-worker/archive"
    ]


@pytest.mark.parametrize(
    "native_worker", ["resolved_readback", "live_root_readback", "live_root_effort"], indirect=True
)
async def test_recorded_builtin_readback_passes_real_allocation_and_session_checks(native_worker):
    from daimon.core.headless_runner import run_worker

    request, root, claimed, trace, _ = native_worker
    result = await run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Read the facts",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    allocated = next(
        call.json()
        for call in trace.requests
        if call.method == "POST" and call.path == "/v1/agents"
    )
    if root.spec.extensions:
        assert allocated["model"] == {
            "id": "claude-haiku-5-5",
            "speed": "standard",
            "effort": {"type": "medium"},
        }
    tools = allocated["tools"]
    assert tools[0]["default_config"]["enabled"] is False
    assert {config["name"] for config in tools[0]["configs"]} == {
        tool.name for tool in root.spec.tools
    }
    assert all(config["enabled"] for config in tools[0]["configs"])
    assert len(allocated["skills"]) == 11 and not allocated.get("mcp_servers")
    assert any(call.method == "POST" and call.path == "/v1/sessions" for call in trace.requests)
    trace.assert_consumed()


@pytest.mark.parametrize("native_worker", ["resolved_readback"], indirect=True)
async def test_resolved_readback_widening_refuses_before_native_session_allocation(native_worker):
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn.errors import AdmissionDenied

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = "builtin_widening"
    with pytest.raises(AdmissionDenied):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Read the facts",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    assert any(call.method == "POST" and call.path == "/v1/agents" for call in trace.requests)
    assert not any(call.method == "POST" and call.path == "/v1/sessions" for call in trace.requests)


@pytest.mark.parametrize("native_worker", ["resolver_fresh", "clone_source"], indirect=True)
async def test_worker_allocation_keeps_fresh_mentions_on_original_agent(native_worker):
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
    from daimon.core.headless_runner import run_worker
    from daimon.core.ma_resolver import new_resolver_cache, resolve_agent

    request, root, claimed, trace, _ = native_worker
    result = await run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Complete the task",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    payload = next(
        call.json()
        for call in trace.requests
        if call.method == "POST" and call.path == "/v1/agents"
    )
    expected_name = f"fleet-{claimed.run_id}-{claimed.ordinal}"
    assert payload["name"] == payload["metadata"][MA_METADATA_KEY_NAME] == expected_name
    assert payload["metadata"] == {
        **root.spec.metadata,
        MA_METADATA_KEY_NAME: expected_name,
        "daimon_managed": "false",
    }
    assert root.spec.metadata["daimon_managed"] == "true"
    assert root.spec.metadata[MA_METADATA_KEY_NAME] != expected_name

    async def unexpected_apply():
        pytest.fail("original source disappeared from resolver index")

    # A new channel mention has no cached native id. List the original and
    # the newly allocated worker through the real SDK/resolver path.
    selected = await resolve_agent(
        request.deps.anthropic,
        tenant_id=request.tenant_id,
        daimon_tag=root.spec.metadata[MA_METADATA_KEY_NAME],
        apply_callable=unexpected_apply,
        cache=new_resolver_cache(),
    )
    assert selected == request.admission.agent.id

    from daimon.core.defaults.sweep import sweep_removed_agents

    # Scheduler startup must not archive the live worker as a deleted default.
    assert (
        await sweep_removed_agents(
            request.deps.anthropic,
            present_names={root.spec.metadata[MA_METADATA_KEY_NAME]},
            tenant_id=request.tenant_id,
            dry_run=False,
        )
        == []
    )


@pytest.mark.parametrize("native_worker", ["clone_source_effort"], indirect=True)
async def test_a_clone_worker_whose_source_has_native_controls_runs(native_worker):
    """RESCORE-2 J4: fleet-claude (its own model controls) failed extensions:dict_type."""
    from daimon.core.headless_runner import run_worker

    request, root, claimed, trace, _ = native_worker
    assert root.spec.extensions and claimed.spec["extensions"]
    result = await run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Complete the task",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    allocated = next(
        call.json()
        for call in trace.requests
        if call.method == "POST" and call.path == "/v1/agents"
    )
    # The source's own controls reach the worker, from the source and not an override.
    assert allocated["model"]["effort"] == {"type": "medium"}


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TypeError("a bug on our side"), "setup_error"),
        (__import__("httpx").ConnectError("provider unreachable"), "provider_error"),
    ],
)
async def test_real_run_worker_records_why_it_failed_before_native_start(
    native_worker, monkeypatch, error, code
):
    """RESCORE-2 J4: run_worker's own handler recorded every pre-native failure as
    preparation_refused, so a bug read on the card as the model being unable to run."""
    from daimon.core import headless_runner
    from daimon.core.fleet import worker_error_text

    request, root, claimed, trace, _ = native_worker

    async def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(headless_runner, "capture_preparation_source", broken)
    with pytest.raises(type(error)):
        await headless_runner.run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    _, rows = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=claimed.tenant_id, run_id=claimed.run_id
    )
    (ended,) = [row for row in rows if row.id == claimed.id]
    assert ended.status == "failed" and ended.error_code == code
    assert "on that model" not in (worker_error_text(ended.error_code) or "")
    assert not any(call.method == "POST" for call in trace.requests)


async def test_a_task_worker_runs_when_the_provider_echoes_its_default_model_controls(
    native_worker,
):
    """SMOKE-1 J3: an Opus task worker (model controls dropped) was refused because MA
    echoed its default speed/effort, and its allocated agent was left behind."""
    from daimon.core.headless_runner import run_worker

    request, root, claimed, trace, controls = native_worker
    assert not claimed.spec.get("extensions")  # the worker declares no model controls
    controls["fault"] = "echo_default_controls"
    result = await run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Complete the task",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    assert not any(call.path.endswith("/new-worker/archive") for call in trace.requests)


async def _refused_and_archived(native_worker, fault):
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn.errors import AdmissionDenied

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = fault
    with pytest.raises(AdmissionDenied):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    assert not any(call.method == "POST" and call.path == "/v1/sessions" for call in trace.requests)
    archived = [call.path for call in trace.requests if call.path.endswith("/archive")]
    assert archived == ["/v1/agents/new-worker/archive"]


async def test_an_echoed_default_on_a_different_model_is_still_refused(native_worker):
    """#1380 review: only the model id may differ in form, never in value."""
    request, root, claimed, trace, controls = native_worker
    assert not claimed.spec.get("extensions")
    await _refused_and_archived(native_worker, "echo_other_model")


@pytest.mark.parametrize("native_worker", ["live_root_effort"], indirect=True)
async def test_a_spec_with_model_controls_needs_an_exact_echo(native_worker):
    """#1380 review: the id-only comparison applies only when the spec declares no controls."""
    request, root, claimed, trace, controls = native_worker
    assert root.spec.extensions  # the worker asked for effort "medium"
    await _refused_and_archived(native_worker, "echo_other_effort")


async def test_an_unknown_agent_create_outcome_never_archives(native_worker):
    """#1380 review: a lost ACK is retried and must find the same agent, so nothing archives."""
    from daimon.core.headless_runner import run_worker
    from mux.errors import ProviderError

    request, root, claimed, trace, controls = native_worker
    controls["fault"] = "lost_ack"
    with pytest.raises((ProviderError, Exception)):
        await run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    assert sum(call.method == "POST" and call.path == "/v1/agents" for call in trace.requests) == 1
    assert not any(call.path.endswith("/archive") for call in trace.requests)
