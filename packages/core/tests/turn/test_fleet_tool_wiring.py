"""Concrete fleet result/contract reads over real SDK worker journals."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from daimon.core._models import FleetWorker as WorkerRow
from daimon.core._models import PlatformPrincipal
from daimon.core.fleet import result_digest
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused, HostFleetPorts
from daimon.core.headless_runner import run_worker
from daimon.core.neutral_resources import resolve_worker_spec
from daimon.core.stores import access_policy, fleet
from daimon.core.turn.runtime_manifest import capture_preparation_source
from daimon.testing.isolated_sdk import build_async_openai
from mux.contracts.config import BackendConfig
from mux.contracts.events import AgentMessagePayload, Event, NativeProvenance, TextPart
from mux.contracts.resources import AgentPatch, AgentSpec
from mux.drivers.openai import deployment as native_transport
from mux.drivers.openai.transport import SDKTransport
from sqlalchemy import update

from ..test_fleet_budget import POLICY, setup, tariffs
from ..test_fleet_budget import sm as sm
from . import test_openai_host as native_host
from .test_openai_worker_plan import application as application
from .test_openai_worker_plan import claimed_openai_worker as claimed_openai_worker
from .test_openai_worker_plan import db_preflight as db_preflight
from .test_openai_worker_plan import db_session_factory as db_session_factory
from .test_openai_worker_plan import owned as owned
from .test_openai_worker_plan import owned_database_preflight as owned_database_preflight
from .test_openai_worker_plan import provider as provider


async def test_real_worker_journal_collect_refuses_absent_required_review(
    claimed_openai_worker, monkeypatch
):
    request, root, worker, prices, _, _, wire = claimed_openai_worker
    native_handle = wire.handle
    full_text = "authoritative input " * 300

    def handle(call):
        response = native_handle(call)
        if call.method == "POST" and call.url.path.endswith("/events"):
            wire.items[-1]["content"][0]["text"] = full_text
        return response

    monkeypatch.setattr(wire, "handle", handle)
    # Configure the stored result policy before execution; native output, result
    # publication, private binding and financial admission remain product paths.
    async with request.deps.sessionmaker.begin() as db:
        await db.execute(
            update(WorkerRow)
            .where(WorkerRow.id == worker.id)
            .values(result_contract={"format": "text", "review_required": True})
        )
    worker = replace(worker, result_contract={"format": "text", "review_required": True})
    async with request.deps.sessionmaker.begin() as db:
        db.add(
            PlatformPrincipal(
                tenant_id=request.tenant_id,
                account_id=root.fleet.account_id,
                platform=request.platform,
                external_id=request.external_user_id,
            )
        )
    root = replace(root, subject=replace(root.subject, platform_user_id=request.external_user_id))
    request = replace(
        request,
        admission=replace(
            request.admission, grant=replace(request.admission.grant, subject=root.subject)
        ),
    )
    result = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Complete the task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    ports = HostFleetPorts(request.deps.sessionmaker, prices=prices, markup=Decimal(1))
    orchestrator = FleetOrchestrator(ports)
    run, _ = await ports.read_run(root.fleet.scope, worker.run_id)
    async with request.deps.sessionmaker() as db:
        policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
    recorded = await ports.read_result(root, policy, run, result)
    assert ports.validate_contract(result.result_contract, recorded) == full_text
    assert result.result.excerpt == full_text[:4096]
    assert len(full_text) > len(result.result.excerpt)
    assert result.result.digest == result_digest(dict(recorded.payload))
    assert await orchestrator.collect(root, policy, run_id=run.id) == {
        "run_id": str(run.id),
        "status": "blocked",
        "reason": "review_required",
    }
    with pytest.raises(FleetRefused, match="result_unavailable"):
        await ports.read_result(
            root, policy, run, replace(result, result=replace(result.result, digest="wrong"))
        )
    with pytest.raises(FleetRefused, match="foreign_run"):
        await ports.read_run(root.fleet.scope.model_copy(update={"account_id": "foreign"}), run.id)
    async with request.deps.sessionmaker.begin() as db:
        await db.execute(
            update(WorkerRow).where(WorkerRow.id == worker.id).values(binding_id="foreign-binding")
        )
    run, workers = await ports.read_run(root.fleet.scope, worker.run_id)
    with pytest.raises(FleetRefused) as refused:
        await ports.read_result(root, policy, run, workers[0])
    assert str(refused.value) == "fleet: result_evidence_invalid"


@pytest.mark.parametrize(
    "format_,text", [("json", "NaN"), ("json", "1e999"), ("json", '{"a":1,"a":2}')]
)
def test_contract_validator_refuses_noncanonical_json(format_, text):
    ports = HostFleetPorts(None, prices={}, markup=Decimal(1))
    event = Event(
        id="result",
        session_id="session",
        sequence=0,
        type="agent.message",
        observed_at=datetime.now(UTC),
        authority="record",
        payload=AgentMessagePayload(
            item_id="answer", content=(TextPart(text=text),), complete=True
        ).model_dump(mode="json"),
        native=NativeProvenance(provider="openai", api_revision="offline"),
    )
    with pytest.raises(FleetRefused, match="result_contract_invalid"):
        ports.validate_contract({"format": format_}, event)


@pytest.mark.parametrize("interrupted", (False, True))
async def test_pending_admission_replay_dispatches_until_the_worker_is_claimed(sm, interrupted):
    parent, run = await setup(sm, provider="openai", funds="10", cap="2")
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=run.tenant_id, policy=POLICY)
    delivered = []
    attempts = 0

    async def dispatch(parent, worker, resolved, reviewed):
        # Observe actual committed admission, without asserting native execution.
        nonlocal attempts
        attempts += 1
        current_run, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
        assert workers == (worker,)
        assert current_run.reserved_usd >= worker.budget_usd
        if interrupted and attempts == 1:
            raise RuntimeError("dispatch interrupted after commit")
        claimed = await fleet.claim_worker(sm, tenant_id=run.tenant_id, run_id=run.id, cap=1)
        assert claimed is not None and claimed.id == worker.id
        delivered.append(worker.id)

    ports = HostFleetPorts(sm, prices=tariffs(parent), markup=Decimal(1), dispatch=dispatch)
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": Decimal(1)}),
    )
    arguments = dict(
        run_id=run.id,
        operation_key="once",
        role="review the input",
        session=None,
        result_contract={"format": "text"},
        reviewer_of=None,
        reviewed_result=None,
    )
    if interrupted:
        with pytest.raises(RuntimeError, match="dispatch interrupted"):
            await ports.admit(parent, resolved, **arguments)
        _, pending = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
        assert pending[0].status == "pending"
    first = await ports.admit(parent, resolved, **arguments)
    second = await ports.admit(parent, resolved, **arguments)
    assert first[1].id == second[1].id and second[1].status == "starting"
    assert delivered == [first[1].id] and attempts == 1 + int(interrupted)
    with pytest.raises(FleetRefused) as refused:
        await ports.admit(parent, resolved, **(arguments | {"role": "changed input"}))
    assert str(refused.value) == "fleet: operation_conflict"
    assert delivered == [first[1].id]
    foreign = replace(
        resolved,
        permissions=replace(
            resolved.permissions, fleet=replace(resolved.permissions.fleet, depth=0)
        ),
    )
    with pytest.raises(FleetRefused) as refused:
        await ports.admit(parent, foreign, **(arguments | {"operation_key": "foreign"}))
    assert str(refused.value) == "fleet: admission_evidence_invalid"


async def test_refused_funding_commits_the_refusal_and_never_dispatches(sm):
    parent, run = await setup(sm, provider="openai", funds="0", cap="2")
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=run.tenant_id, policy=POLICY)

    async def forbidden(*args):
        pytest.fail("refused funding reached native dispatch")

    ports = HostFleetPorts(sm, prices=tariffs(parent), markup=Decimal(1), dispatch=forbidden)
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": Decimal(1)}),
    )
    for _ in range(2):
        with pytest.raises(FleetRefused, match="financial_admission_refused"):
            await ports.admit(
                parent,
                resolved,
                run_id=run.id,
                operation_key="refused",
                role="worker",
                session=None,
                result_contract=None,
                reviewer_of=None,
                reviewed_result=None,
            )
    _, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert len(workers) == 1 and workers[0].status == "cancelled"


@pytest.mark.parametrize("contract", ({"format": "xml"}, {"schema": "remote"}, {"format": []}))
async def test_unsupported_contract_refuses_spawn_before_funding(sm, contract):
    parent, run = await setup(sm, provider="openai", funds="10", cap="2")
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=run.tenant_id, policy=POLICY)

    async def forbidden(*args):
        pytest.fail("unsupported contract reached dispatch")

    ports = HostFleetPorts(sm, prices=tariffs(parent), markup=Decimal(1), dispatch=forbidden)
    with pytest.raises(FleetRefused, match="result_contract_unsupported"):
        await FleetOrchestrator(ports).spawn(
            parent,
            POLICY,
            now=datetime.now(UTC),
            operation_key="invalid-contract",
            role="worker",
            backend=parent.backend_grants[0],
            limits=parent.limits.model_copy(update={"budget_usd": Decimal(1)}),
            agent=parent.spec,
            result_contract=contract,
        )
    retained, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert workers == () and retained.reserved_usd == 0


@pytest.mark.parametrize("verdict", ("approved", "wrong_digest", "refused", "wrong_contract"))
async def test_required_review_gate_uses_real_spawn_dispatch_and_full_journal(
    claimed_openai_worker, monkeypatch, verdict
):
    request, root, worker, prices, owned, calls, primary_wire = claimed_openai_worker
    async with request.deps.sessionmaker.begin() as db:
        db.add(
            PlatformPrincipal(
                tenant_id=request.tenant_id,
                account_id=root.fleet.account_id,
                platform=request.platform,
                external_id=request.external_user_id,
            )
        )
    root = replace(root, subject=replace(root.subject, platform_user_id=request.external_user_id))
    request = replace(
        request,
        admission=replace(
            request.admission, grant=replace(request.admission.grant, subject=root.subject)
        ),
    )
    source = await capture_preparation_source(
        replace(request, scope=root.fleet.scope, worker=None, source=None)
    )
    assert source.source is not None
    definition = AgentSpec.model_validate(worker.spec)
    clone = request.worker.source_agent is not None
    resolved_primary = resolve_worker_spec(
        root,
        source.source if clone else definition,
        overrides=AgentPatch.model_validate(worker.spec) if clone else None,
        policy=POLICY,
        now=request.now(),
        backend=request.worker.backend,
        limits=root.limits.model_copy(update={"budget_usd": Decimal("0.25")}),
        captured_source=source,
    )
    request = replace(
        request, worker=resolved_primary, scope=resolved_primary.permissions.fleet.scope
    )
    contract = {"format": "text", "review_required": True}
    async with request.deps.sessionmaker.begin() as db:
        await db.execute(
            update(WorkerRow)
            .where(WorkerRow.id == worker.id)
            .values(
                result_contract=contract,
                budget_usd=Decimal("0.25"),
                spec=resolved_primary.spec.model_dump(mode="json"),
                spec_digest=resolved_primary.digest,
            )
        )
    worker = replace(
        worker,
        result_contract=contract,
        budget_usd=Decimal("0.25"),
        spec=resolved_primary.spec.model_dump(mode="json"),
        spec_digest=resolved_primary.digest,
    )
    primary = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Produce the full result",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert primary.status == "succeeded" and primary.result is not None
    primary_text = primary_wire.items[-1]["content"][0]["text"]
    review_text = json.dumps(
        {
            "verdict": "refused" if verdict == "refused" else "approved",
            "reviewed_digest": "wrong" if verdict == "wrong_digest" else primary.result.digest,
        }
    )
    # A second native session and agent, supplied by transport only. Both workers
    # use real financial admission, pool claims, preparation and result publication.
    monkeypatch.setattr(native_host, "SESSION_ID", "native-review-session")
    wire = native_host.Wire()
    wire.expected_spend_limit = None
    agents = {owned.retained.agent.ref.id: owned.wire.agent}
    session = wire.session
    monkeypatch.setattr(
        wire,
        "session",
        lambda: {**session(), "agent": {**session()["agent"], "id": "review-agent"}},
    )

    def native(call):
        calls.append(call)
        path = call.url.path
        if path == "/v1/agents" and call.method == "POST":
            agents["review-agent"] = {
                **json.loads(call.content),
                "id": "review-agent",
                "created_at": 1791792000,
            }
            return httpx.Response(200, json=agents["review-agent"])
        if path in {"/v1/agents/" + key for key in agents}:
            return httpx.Response(200, json=agents[path.rsplit("/", 1)[1]])
        if path == "/v1/agents/environments/templates/owned-environment":
            return httpx.Response(
                200,
                json={
                    **owned.wire.environment,
                    "network": {"access": "enabled"},
                    "packages": {"system": [], "python": [], "npm": []},
                },
            )
        if path == "/v1/agents/sessions" and call.method == "POST":
            assert json.loads(call.content)["agent_id"] == "review-agent"
            return httpx.Response(200, json=wire.session())
        response = wire.handle(call)
        for turn in wire.turns:
            turn["agent_id"] = "review-agent"
        if call.method == "POST" and path.endswith("/events"):
            wire.items[-1]["content"][0]["text"] = review_text
        return response

    dispatched = []

    async def dispatch(parent, committed, resolved, reviewed):
        assert reviewed is not None and reviewed.session_id == primary.result.session_id
        assert reviewed.payload["content"][0]["text"] == primary_text
        assert result_digest(dict(reviewed.payload)) == primary.result.digest
        assert committed.result_contract == {
            "format": "review",
            "reviewed_digest": primary.result.digest,
        }
        claimed = await fleet.claim_worker(
            request.deps.sessionmaker, tenant_id=committed.tenant_id, run_id=committed.run_id, cap=2
        )
        assert claimed is not None and claimed.id == committed.id
        selected = replace(request, worker=resolved, scope=resolved.permissions.fleet.scope)
        result = await run_worker(
            selected,
            parent=parent,
            worker=claimed,
            trigger_message="Review this full result: " + json.dumps(dict(reviewed.payload)),
            prices=prices,
            cancel=asyncio.Event(),
        )
        assert result.status == "succeeded" and result.result is not None
        assert result.result.session_id != primary.result.session_id
        dispatched.append(result)

    ports = HostFleetPorts(
        request.deps.sessionmaker, prices=prices, markup=Decimal("0.25"), dispatch=dispatch
    )
    orchestrator = FleetOrchestrator(ports)
    async with request.deps.sessionmaker() as db:
        policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
    async with build_async_openai(
        transport=httpx.MockTransport(native),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        monkeypatch.setattr(
            native_transport, "configured_transport", lambda **kwargs: SDKTransport(sdk)
        )
        reply = await orchestrator.spawn(
            root,
            policy,
            now=request.now(),
            operation_key="review-primary",
            role="review the full journal",
            backend=BackendConfig.model_validate(
                request.admission.backend_revision.model_dump(
                    include=set(BackendConfig.model_fields)
                )
            ),
            limits=root.limits.model_copy(update={"budget_usd": Decimal("0.25")}),
            agent=root.spec.model_copy(update={"name": "reviewer", "declared_drops": ("memory",)}),
            reviewer_of=primary.id,
            captured_source=source,
        )
    assert len(dispatched) == 1 and reply["worker_id"] == str(dispatched[0].id)
    if verdict == "wrong_contract":
        # Persisted contract tampering must not certify the actual review journal.
        async with request.deps.sessionmaker.begin() as db:
            await db.execute(
                update(WorkerRow)
                .where(WorkerRow.id == dispatched[0].id)
                .values(result_contract={"format": "review", "reviewed_digest": "wrong"})
            )
        with pytest.raises(FleetRefused, match="review_digest_mismatch"):
            await orchestrator.collect(root, policy, run_id=primary.run_id)
    else:
        result = await orchestrator.collect(root, policy, run_id=primary.run_id)
        if verdict == "approved":
            assert result == {
                "run_id": str(primary.run_id),
                "status": "ready",
                "results": [{"worker_id": str(primary.id), "result": primary_text}],
            }
        else:
            assert result == {
                "run_id": str(primary.run_id),
                "status": "blocked",
                "reason": "review_refused",
            }


@pytest.mark.parametrize("rollback", (False, True))
async def test_real_worker_result_and_completion_wake_share_the_transaction(
    claimed_openai_worker, monkeypatch, rollback
):
    from uuid import uuid4

    from daimon.core.continuity import wakes
    from daimon.core.continuity.continuation import ContinuationRequest
    from daimon.core.fleet import FleetConflict
    from daimon.core.stores.task_continuations import list_pending_continuations

    request, root, worker, prices, _, _, _ = claimed_openai_worker
    run, _ = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=worker.run_id
    )
    continuation = ContinuationRequest(
        tenant_id=run.tenant_id,
        platform=run.platform,
        parent_channel_id=run.channel_id,
        thread_id=run.thread_id,
        requester_account_id=run.orchestrator_account_id,
        requester_external_user_id=root.subject.platform_user_id,
        target_ma_agent_id=request.admission.agent.id,
        target_name=request.admission.config.agent_name,
        requested_work=None,
        reason="worker_finished",
        idempotency_key=uuid4(),
    )
    continuation = continuation.model_copy(
        update={"idempotency_key": wakes.worker_finished_key(run.id, (worker.id,))}
    )
    async with request.deps.sessionmaker.begin() as db:
        await fleet.lock_budget(
            db, tenant_id=run.tenant_id, actor_id=run.orchestrator_account_id, run_id=run.id
        )
        await wakes.arm_worker_finished(
            db, continuation, run_id=run.id, worker_ids=(worker.id,), now=request.now()
        )
    publish = wakes.enqueue_worker_finished
    seen = []

    async def checked(db, **kwargs):
        _, rows = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
        seen.append(rows[0].status)
        assert rows[0].status == "succeeded"
        offered = await publish(db, **kwargs)
        assert offered
        if rollback:
            raise FleetConflict("publication rollback")
        return offered

    monkeypatch.setattr("daimon.core.headless_runner.enqueue_worker_finished", checked)
    call = run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Complete the task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    if rollback:
        with pytest.raises(FleetConflict):
            await call
    else:
        assert (await call).status == "succeeded"
    assert seen == ["succeeded"]  # mark_running never invokes the terminal publisher.
    async with request.deps.sessionmaker() as db:
        ready = await list_pending_continuations(
            db, tenant_id=run.tenant_id, platform=run.platform, thread_id=run.thread_id
        )
    _, rows = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=run.tenant_id, run_id=run.id
    )
    assert bool(ready) is not rollback
    assert rows[0].status == ("running" if rollback else "succeeded")
    if rollback:
        assert rows[0].result is None and rows[0].binding_id is not None
        assert rows[0].reserved_usd > 0
