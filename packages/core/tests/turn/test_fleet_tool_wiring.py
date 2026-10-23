"""Concrete fleet result/contract reads over real SDK worker journals."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core._models import FleetWorker as WorkerRow
from daimon.core._models import PlatformPrincipal
from daimon.core.fleet import result_digest
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused, HostFleetPorts
from daimon.core.headless_runner import run_worker
from daimon.core.neutral_resources import resolve_worker_spec
from daimon.core.stores import access_policy, fleet
from daimon.core.turn.runtime_manifest import capture_preparation_source
from mux.contracts.config import BackendConfig
from mux.contracts.events import AgentMessagePayload, Event, NativeProvenance, TextPart
from mux.contracts.resources import AgentPatch, AgentSpec
from sqlalchemy import update

from ..test_fleet_budget import POLICY, setup, tariffs
from ..test_fleet_budget import sm as sm
from .test_native_session_receipts import native_worker as native_worker
from .test_native_session_receipts import source_read_only as source_read_only
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


async def test_same_backend_review_preserves_real_primary_journal_without_admission(
    claimed_openai_worker,
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

    async def forbidden(*args):
        pytest.fail("same-backend review reached native dispatch")

    ports = HostFleetPorts(
        request.deps.sessionmaker, prices=prices, markup=Decimal("0.25"), dispatch=forbidden
    )
    orchestrator = FleetOrchestrator(ports)
    async with request.deps.sessionmaker() as db:
        policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
    before = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=primary.tenant_id, run_id=primary.run_id
    )
    calls_before = len(calls)
    with pytest.raises(FleetRefused, match="review_backend_must_differ"):
        await orchestrator.spawn(
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
    after = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=primary.tenant_id, run_id=primary.run_id
    )
    assert after == before
    assert len(calls) == calls_before
    assert await orchestrator.collect(root, policy, run_id=primary.run_id) == {
        "run_id": str(primary.run_id),
        "status": "blocked",
        "reason": "review_required",
    }


@pytest.mark.parametrize("row", ("pinned", "template"))
@pytest.mark.parametrize("verdict", ("approved", "refused", "wrong_digest", "wrong_contract"))
async def test_cross_backend_review_gate_uses_real_sdk_dispatch_and_journal(
    native_worker, monkeypatch, tmp_path, verdict, row
):
    from datetime import timedelta

    import httpx
    import mux.drivers.openai as deployment
    from daimon.core._models import FleetRun as RunRow
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.neutral_resources import WorkerLimits
    from daimon.core.pricing import provider_prices
    from daimon.core.stores import mux_state

    from .test_openai_host import openai_native_spend_cap

    # The review worker carries a native budget: a project that honours it.
    openai_native_spend_cap(monkeypatch)
    from daimon.core.turn.openai_deployment import OpenAIDeployment
    from daimon.core.turn.runtimes import build_channel_runtime
    from mux.contracts.config import ConfigRevision, resolve_default
    from mux.contracts.ids import ModelRef
    from mux.drivers.openai.transport import SDKTransport
    from mux.testing.isolated_sdk import build_async_openai

    from .test_openai_conversation_worker import ConversationWire

    request, root, claimed, native_trace, _ = native_worker
    # Fund both retained attempts; the primary fixture alone owns the original $1 cap.
    root = replace(
        root,
        limits=root.limits.model_copy(update={"budget_usd": Decimal("2")}),
        subject=replace(root.subject, via_agent_key=False),
    )
    request = replace(
        request,
        admission=replace(
            request.admission, grant=replace(request.admission.grant, subject=root.subject)
        ),
    )
    contract = {"format": "text", "review_required": True}
    async with request.deps.sessionmaker.begin() as db:
        await db.execute(
            update(WorkerRow).where(WorkerRow.id == claimed.id).values(result_contract=contract)
        )
        await db.execute(
            update(RunRow)
            .where(RunRow.id == claimed.run_id)
            .values(max_workers=2, total_budget_usd=Decimal("2"))
        )
        db.add(
            PlatformPrincipal(
                tenant_id=request.tenant_id,
                account_id=root.fleet.account_id,
                platform=request.platform,
                external_id=request.external_user_id,
            )
        )
    claimed = replace(claimed, result_contract=contract)
    primary = await run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Produce findings",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert primary.status == "succeeded" and primary.result is not None
    assert primary.backend == "anthropic" and primary.result.excerpt == "verified-worker-result"
    captured = await capture_preparation_source(
        replace(request, scope=root.fleet.scope, worker=None, source=None)
    )
    assert captured.source is not None
    model = ModelRef(provider="openai", id="gpt-6-luna")
    backend = resolve_default(
        BackendConfig(backend="openai", profile="openai.conversation_only", model=model.id)
    )
    root = replace(
        root, backend_grants=(*root.backend_grants, backend), models=(*root.models, model)
    )
    prices = {**tariffs(root), **provider_prices(backend.profile)}
    text = json.dumps(
        {
            "verdict": "refused" if verdict == "refused" else "approved",
            "reviewed_digest": "wrong" if verdict == "wrong_digest" else primary.result.digest,
        }
    )

    class ReviewWire(ConversationWire):
        def handle(self, call):
            response = super().handle(call)
            if call.method == "POST" and call.url.path.endswith("/agents/sessions"):
                self.items[-1]["content"][0]["text"] = text
            return response

    wire = ReviewWire()
    # A worker TEMPLATE ("*" source, any channel/account) serves the same worker
    # as a row pinned to this agent version and channel.
    template = row == "template"
    plan = OpenAIDeployment(
        profile=backend.profile,
        tenant_id=root.fleet.scope.tenant_id,
        platform="*" if template else request.platform,
        channel_id="*" if template else root.fleet.root.channel.channel_id,
        account_id="*" if template else root.fleet.scope.account_id,
        project="offline-project",
        api_key_env="I4_OFFLINE_OPENAI_KEY",
        worker_materialization=True,
        source_agent_id="*"  # pyright: ignore[reportArgumentType]
        if template
        else derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=captured.source.ref.id),
        source_agent_version="*"  # pyright: ignore[reportArgumentType]
        if template
        else captured.source.revision.local,
        configuration_grant={
            "profiles": [backend.profile],
            "models": [model.id],
            "thread_modes": [backend.thread_mode],
            "requires": dict(backend.requires),
        },
    )
    manifest = tmp_path / "review-runtime.json"
    manifest.write_text(json.dumps([plan.model_dump(mode="json")]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    monkeypatch.setenv(plan.api_key_env, "offline")
    dispatched = []

    async def dispatch(parent, committed, resolved, reviewed):
        assert committed.backend == "openai" != primary.backend
        assert reviewed is not None and reviewed.session_id == primary.result.session_id
        assert reviewed.payload["content"][0]["text"] == "verified-worker-result"
        assert result_digest(dict(reviewed.payload)) == primary.result.digest
        assert committed.result_contract == {
            "format": "review",
            "reviewed_digest": primary.result.digest,
        }
        retained, rows = await fleet.run_status(
            request.deps.sessionmaker, tenant_id=primary.tenant_id, run_id=primary.run_id
        )
        assert committed in rows and retained.reserved_usd >= committed.budget_usd
        worker = await fleet.claim_worker(
            request.deps.sessionmaker, tenant_id=committed.tenant_id, run_id=committed.run_id, cap=2
        )
        assert worker is not None and worker.id == committed.id
        revision = ConfigRevision.create(root.fleet.root.channel, 2, resolved.backend)
        async with request.deps.sessionmaker.begin() as db:
            await mux_state.put_config_revision(db, revision, origin="agent")
        selected = replace(
            request,
            worker=resolved,
            scope=resolved.permissions.fleet.scope,
            deps=replace(
                request.deps, turn_runtime_factory=build_channel_runtime, turn_runtimes={}
            ),
            source=None,
            counterpart=None,
            verified_counterpart=None,
            admission=replace(
                request.admission,
                backend_revision=revision,
                backend_agent_revision=None,
                backend=None,
            ),
        )
        result = await run_worker(
            selected,
            parent=parent,
            worker=worker,
            trigger_message="Review the full result: " + json.dumps(dict(reviewed.payload)),
            prices=prices,
            cancel=asyncio.Event(),
        )
        assert result.status == "succeeded" and result.result is not None and result.turns_used == 1
        assert result.result.session_id != primary.result.session_id
        dispatched.append(result)

    ports = HostFleetPorts(
        request.deps.sessionmaker, prices=prices, markup=Decimal(1), dispatch=dispatch
    )
    owner = FleetOrchestrator(ports)
    async with request.deps.sessionmaker() as db:
        policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        monkeypatch.setattr(deployment, "configured_transport", lambda **kwargs: SDKTransport(sdk))
        reply = await owner.spawn(
            root,
            policy,
            now=request.now(),
            operation_key="cross-backend-review",
            role="review the full journal",
            backend=BackendConfig.model_validate(backend.model_dump()),
            limits=WorkerLimits(
                budget_usd=Decimal("0.055"),
                deadline=min(root.limits.deadline, request.now() + timedelta(seconds=60)),
                max_turns=1,
                native_budget_usd=Decimal("0.01"),
            ),
            agent=root.spec.model_copy(
                update={
                    "model": model,
                    "tools": (),
                    "skills": (),
                    "mcp_servers": (),
                    "extensions": None,
                    "declared_drops": (
                        "memory",
                        "sandbox",
                        "tools",
                        "skills",
                        "mcp",
                        "files",
                        "repository",
                    ),
                }
            ),
            reviewer_of=primary.id,
            captured_source=captured,
        )

    assert len(dispatched) == 1 and reply["worker_id"] == str(dispatched[0].id)
    assert [call.url.path for call in wire.posts] == ["/v1/agents", "/v1/agents/sessions"]
    assert any(
        call.method == "POST" and call.path.endswith("/events") for call in native_trace.requests
    )
    assert text == dispatched[0].result.excerpt
    if verdict == "wrong_contract":
        async with request.deps.sessionmaker.begin() as db:
            await db.execute(
                update(WorkerRow)
                .where(WorkerRow.id == dispatched[0].id)
                .values(result_contract={"format": "review", "reviewed_digest": "wrong"})
            )
        with pytest.raises(FleetRefused, match="review_digest_mismatch"):
            await owner.collect(root, policy, run_id=primary.run_id)
    else:
        result = dict(await owner.collect(root, policy, run_id=primary.run_id))
        if verdict == "approved":
            assert str(result.pop("cost")).startswith("This cost "), "the reply's cost line"
            result.pop("cost_usd")
            assert result == {
                "run_id": str(primary.run_id),
                "status": "ready",
                "results": [{"worker_id": str(primary.id), "result": "verified-worker-result"}],
            }
        else:
            assert result == {
                "run_id": str(primary.run_id),
                "status": "blocked",
                "reason": "review_refused",
            }
            assert "results" not in result

    from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCard

    async def load_parent(interaction):
        return root, policy

    async def unexpected_display(*args, **kwargs):
        pytest.fail("summary authorization must not post or edit a platform card")

    card = DiscordFleetCard(
        run_id=primary.run_id,
        service=owner,
        load_parent=load_parent,
        send=unexpected_display,
        edit=unexpected_display,
    )
    assert await card.allow_summary() is (verdict == "approved")


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
