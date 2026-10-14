"""Exact prepared-root joins over the real origin, operation and binding owners."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from daimon.core._models import FleetRun, MuxOperation
from daimon.core.authz import Subject, Surface, build_agent_ref
from daimon.core.channel_backend import AgentBackendService, BackendActor, load_backend_agent
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.neutral_resources import AgentFleetGrant, WorkerSpecRefused, fleet_root_permissions
from daimon.core.scope import ResolvedConfig
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
from daimon.core.session_ports_compat import create_session_record
from daimon.core.sessions import project_session_resources
from daimon.core.stores import (
    access_policy,
    agent_memory_stores,
    fleet,
    mux_state,
    turn_card_intents,
    turn_origins,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.persistence import TurnPersistence, UncertainSend
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import freeze_prepared_root, read_frozen_root
from daimon.core.turn_origin import turn_origin
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, send_events_response, sse_response
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle, deps_for
from mux.contracts.resources import ProviderBinding
from mux.errors import OperationConflict, ScopeViolation
from mux.state.store import binding_slot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..test_fleet_permissions import POLICY, parent
from .conftest import make_status_idle


@pytest.fixture
async def root_turn(db_preflight, db_nullpool_engine):
    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, tenant=tenant, account=account, platform="discord", external_id="requester"
        )
        await access_policy.set_access_policy(db, tenant_id=tenant.id, policy=POLICY)
    root = parent("anthropic")
    scope = resource_scope(
        tenant_id=str(tenant.id), account_id=str(account.id), authorization_id="admitted-turn"
    )
    thread = root.fleet.root.model_copy(
        update={"channel": root.fleet.root.channel.model_copy(update={"tenant_id": str(tenant.id)})}
    )
    native = ma_agent(
        id="root-source",
        name="root",
        tenant_id=str(tenant.id),
        system="Root",
        model="claude-haiku-5-5",
    )
    environment = ma_environment(tenant_id=str(tenant.id))
    session_id = "root-session-" + str(uuid4())
    snapshot = ma_session(id=session_id, agent=native, environment_id=environment.id)
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/root-source",
        lambda request, match: httpx.Response(200, json=native.model_dump(mode="json")),
    )
    router.add(
        "GET",
        rf"/v1/environments/{environment.id}",
        lambda request, match: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/sessions",
        lambda request, match: httpx.Response(200, json=snapshot.model_dump(mode="json")),
    )
    input_sent = asyncio.Event()

    def send(request, match):
        input_sent.set()
        return send_events_response()

    class Stream(httpx.AsyncByteStream):
        def __init__(self):
            input_sent.clear()

        async def __aiter__(self):
            await input_sent.wait()
            response = sse_response(
                [make_status_idle(event_id="root-idle-" + str(uuid4())).model_dump(mode="json")]
            )
            yield response.content

    router.add("POST", rf"/v1/sessions/{session_id}/events", send)
    router.add(
        "GET",
        rf"/v1/sessions/{session_id}/events",
        lambda request, match: httpx.Response(200, json={"data": [], "has_more": False}),
    )
    router.add(
        "GET",
        rf"/v1/sessions/{session_id}/events/stream",
        lambda request, match: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        ),
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        captured = await load_backend_agent(
            client, sm, tenant_id=tenant.id, agent_name="root", actual_agent=actual
        )
        async with sm.begin() as db:
            await agent_memory_stores.insert_memory_store(
                db,
                tenant_id=tenant.id,
                agent_id=captured.agent_id,
                memory_store_id="warm-root-memory",
            )
        captured = await project_session_resources(
            captured,
            agent=actual,
            environment=environment,
            session_factory=sm,
            tenant_id=tenant.id,
            agent_uuid=captured.agent_id,
            account_id=account.id,
            resource_scope=scope,
            memory_read_only=True,
        )
        acknowledged = await create_session_record(
            client,
            scope=scope,
            agent=native.id,
            environment_id=environment.id,
            metadata=None,
            resources=None,
        )
        assert acknowledged.agent.id == actual.id
        spec = captured.effective_spec.model_copy(
            update={
                "model": root.spec.model,
                "extensions": None,
                "tools": (),
                "skills": (),
                "mcp_servers": (),
                "declared_drops": (),
            }
        )
        subject = Subject(platform_user_id="requester")
        grant = AdmissionGrant(
            tenant.id,
            subject,
            Surface.CHANNEL,
            root.fleet.place,
            build_agent_ref(actual.name, actual.metadata or {}),
            root.fleet.place,
            thread.channel.channel_id,
            thread.thread_id,
            False,
        )
        admission = Admission(
            account.id,
            actual,
            environment,
            ResolvedConfig(agent_name="root", environment_name="default"),
            grant=grant,
            origin_channel_id=thread.channel.channel_id,
            origin_thread_id=thread.thread_id,
        )
        async with sm.begin() as db:
            intent = await turn_card_intents.create_turn_card_intent(
                db,
                tenant_id=tenant.id,
                platform="discord",
                channel_id=thread.channel.channel_id,
                thread_id=thread.thread_id,
                turn_token=uuid4(),
            )
        async with turn_origin(
            sm,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id=thread.channel.channel_id,
            thread_id=thread.thread_id,
            responder_ma_agent_id=native.id,
            responder_name="root",
            role=Role.USER,
        ) as origin:
            root = replace(
                root,
                fleet=replace(
                    root.fleet, scope=scope, root=thread, origin_id=origin.id, account_id=account.id
                ),
                subject=subject,
                agent=grant.agent,
                spec=spec,
                base_system=spec.system,
                agent_id=captured.agent_id,
                environment=None,
                source_agents=(captured.source.ref,),
                source_revisions=((captured.source.ref, captured.source.revision),),
                limits=root.limits.model_copy(
                    update={"deadline": datetime.now(UTC) + timedelta(seconds=120)}
                ),
            )
            grant_service = AgentBackendService(sm, enabled=True)
            admin = BackendActor(tenant.id, account.id, Subject(is_admin=True))
            admin_value = AgentFleetGrant(
                max_workers=8,
                choices=tuple(
                    (backend, model)
                    for backend in root.backend_grants
                    for model in root.models
                    if backend.backend == model.provider
                ),
                limits=root.limits,
                max_run_seconds=600,
                max_worker_seconds=120,
                max_root_turns=3,
            )
            preview = await grant_service.preview_fleet_grant(
                captured, admin_value, now=datetime.now(UTC)
            )
            await grant_service.set_fleet_grant(
                captured,
                admin_value,
                0,
                admin,
                expected_preview_digest=preview.digest,
                now=datetime.now(UTC),
            )
            store = mux_state.PostgresStateStore(sm)
            binding = ProviderBinding(
                id=str(uuid4()),
                thread=thread,
                provider="anthropic",
                profile="anthropic.managed_agents",
                native_refs={"session": acknowledged.id, "agent": actual.id},
                generation=1,
                config_revision=0,
                legacy_account_id=str(account.id),
            )
            await store.put_binding(binding, expected_generation=0)
            admin_value = await grant_service.read_fleet_grant(
                tenant.id, captured.agent_id, now=datetime.now(UTC)
            )
            assert admin_value is not None
            root = fleet_root_permissions(
                captured,
                grant,
                admin_value,
                fleet=root.fleet,
                policy=POLICY,
                now=datetime.now(UTC),
            )
            grants = [root]

            async def record(*, event):
                return None

            deps = replace(
                deps_for(client, sm), fleet_enabled=True, fleet_root_grants=None, state_store=store
            )
            backend = managed_agents(
                client, scope=scope, resources=frozenset({("session", acknowledged.id)})
            )
            prepared = PreparedTurn(
                admission,
                acknowledged.id,
                None,
                None,
                False,
                account.id,
                record,
                backend=backend,
                session_ref=resource_ref(backend, "session", acknowledged.id, scope=scope),
                _scope=scope,
                _source=captured,
                _preparation=ProviderPreparationRequest(
                    deps,
                    admission,
                    scope,
                    tenant.id,
                    "discord",
                    "requester",
                    thread.thread_id,
                    account.id,
                    False,
                    DEFAULT_MA_CAPABILITIES,
                    None,
                    root.limits.deadline,
                    lambda: datetime.now(UTC),
                ),
            )
            yield deps, prepared, grants, intent, origin, scope, binding, transport


async def freeze(value):
    deps, prepared, _, intent, _, scope, binding, _ = value
    persistence = TurnPersistence(deps.state_store, binding, scope, operation_key="root-test")

    async def write():
        with persistence.activate():
            await freeze_prepared_root(
                deps, prepared, intent_id=intent.id, now=lambda: datetime.now(UTC)
            )

    await persistence.run(write)


async def test_real_native_ack_root_join_is_shared_by_intent_and_origin(root_turn):
    await freeze(root_turn)
    deps, prepared, grants, intent, origin, scope, binding, transport = root_turn
    by_intent, policy = await read_frozen_root(
        deps, scope, intent_id=intent.id, now=datetime.now(UTC)
    )
    by_origin, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    assert by_intent == by_origin
    assert by_intent.fleet.origin_id == origin.id and by_intent.fleet.depth == 0
    assert by_intent.fleet.scope == scope and by_intent.subject == prepared.admission.grant.subject
    assert by_intent.limits == grants[0].limits
    assert by_intent.source_revisions == grants[0].source_revisions
    assert policy == POLICY
    async with deps.sessionmaker() as db:
        retained = await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id)
    assert retained.slot.thread == binding.thread
    assert "source" not in retained.result and "native" not in retained.result
    assert [(r.method, r.path) for r in transport.requests] == [
        ("GET", "/v1/agents/root-source"),
        ("POST", "/v1/sessions"),
    ]


@pytest.mark.parametrize(
    "change",
    [
        "origin_deleted",
        "expired",
        "intent_retired",
        "intent_foreign",
        "responder",
        "binding",
        "principal",
        "run",
        "source_missing",
    ],
)
async def test_root_join_refuses_missing_foreign_stale_and_retargeted_authority(root_turn, change):
    await freeze(root_turn)
    deps, prepared, grants, intent, origin, scope, binding, transport = root_turn
    now = datetime.now(UTC)
    selected_run = None
    async with deps.sessionmaker.begin() as db:
        if change == "origin_deleted":
            await turn_origins.delete_origin(db, origin_id=origin.id)
        elif change == "intent_retired":
            await turn_card_intents.retire_turn_card_intent(
                db, id=intent.id, expected_message_id=None
            )
        elif change == "responder":
            from daimon.core._models import TurnOrigin
            from sqlalchemy import update

            await db.execute(
                update(TurnOrigin)
                .where(TurnOrigin.id == origin.id)
                .values(responder_ma_agent_id="other")
            )
    if change == "expired":
        now = origin.expires_at + timedelta(seconds=1)
    elif change == "intent_foreign":
        intent = intent.model_copy(update={"id": uuid4()})
    elif change == "binding":
        await deps.state_store.put_binding(
            binding.model_copy(update={"generation": 2, "native_refs": {"session": "other"}}),
            expected_generation=1,
        )
    elif change == "principal":
        scope = scope.model_copy(update={"principal_id": "other"})
    elif change == "run":
        selected_run = uuid4()
    elif change == "source_missing":
        prepared = replace(prepared, _source=None)
        with pytest.raises(WorkerSpecRefused):
            await freeze((deps, prepared, grants, intent, origin, scope, binding, transport))
        return

    with pytest.raises((WorkerSpecRefused, ScopeViolation)):
        await read_frozen_root(deps, scope, intent_id=intent.id, run_id=selected_run, now=now)
    assert len(transport.requests) == 2


async def test_repeated_root_capture_never_enlarges_stored_operator_grants(root_turn):
    await freeze(root_turn)
    deps, _, grants, intent, _, scope, _, _ = root_turn
    original, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    grants[0] = replace(
        grants[0], limits=grants[0].limits.model_copy(update={"budget_usd": Decimal("99")})
    )

    async def loader(_grant, _source):
        return grants[0]

    with pytest.raises(WorkerSpecRefused, match="origin: unsupported"):
        await freeze((replace(deps, fleet_root_grants=loader), *root_turn[1:]))
    retained, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    assert retained == original and retained.limits.budget_usd == original.limits.budget_usd


async def test_real_run_retains_root_before_native_input(root_turn):
    deps, prepared, _, intent, origin, scope, _, transport = root_turn

    async def reseed():
        return "root message"

    lifecycle = RecordingLifecycle()
    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=prepared.admission.grant.tenant_id,
        platform="discord",
        thread_id=origin.thread_id,
        external_user_id="requester",
        user_message="root message",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: lifecycle,
        turn_card_intent_id=intent.id,
    )
    retained, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    assert retained.fleet.root.thread_id == origin.thread_id
    assert any(r.method == "POST" and r.path.endswith("/events") for r in transport.requests)


async def test_disabled_root_projection_does_not_read_or_write_a_carrier(root_turn):
    deps, prepared, _, intent, origin, scope, _, transport = root_turn
    deps = replace(deps, fleet_enabled=False)

    async def reseed():
        return "root message"

    lifecycle = RecordingLifecycle()
    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=prepared.admission.grant.tenant_id,
        platform="discord",
        thread_id=origin.thread_id,
        external_user_id="requester",
        user_message="root message",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: lifecycle,
    )
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
    assert any(r.method == "POST" and r.path.endswith("/events") for r in transport.requests)


async def test_fresh_read_authorization_preserves_original_root_scope(root_turn):
    await freeze(root_turn)
    deps, _, _, intent, _, scope, _, _ = root_turn
    fresh = scope.model_copy(update={"authorization_id": "fresh-verified-reader"})
    retained, _ = await read_frozen_root(deps, fresh, intent_id=intent.id, now=datetime.now(UTC))
    assert retained.fleet.scope == scope and retained.fleet.scope != fresh


@pytest.mark.parametrize(
    "fault", ("intent", "origin", "scope", "binding", "requester", "source_scope")
)
async def test_invalid_capture_cannot_commit_a_root_or_dispatch_input(root_turn, fault):
    deps, prepared, grants, intent, origin, scope, binding, transport = root_turn
    if fault == "intent":
        intent = intent.model_copy(update={"id": uuid4()})
    elif fault == "origin":
        async with deps.sessionmaker.begin() as db:
            await turn_origins.delete_origin(db, origin_id=origin.id)
    elif fault == "scope":
        prepared = replace(
            prepared,
            _source=prepared._source.model_copy(
                update={"resource_scope": scope.model_copy(update={"principal_id": "other"})}
            ),
        )
    elif fault == "source_scope":
        prepared = replace(
            prepared,
            _source=prepared._source.model_copy(
                update={"resource_scope": scope.model_copy(update={"authorization_id": "other"})}
            ),
        )
    elif fault == "binding":
        await deps.state_store.put_binding(
            binding.model_copy(update={"generation": 2, "native_refs": {"session": "replacement"}}),
            expected_generation=1,
        )
    else:
        prepared = replace(
            prepared,
            admission=replace(
                prepared.admission,
                grant=replace(prepared.admission.grant, subject=Subject(platform_user_id="other")),
            ),
        )
    with pytest.raises((WorkerSpecRefused, ScopeViolation)):
        await freeze((deps, prepared, grants, intent, origin, scope, binding, transport))
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
    assert len(transport.requests) == 2


async def test_same_intent_cannot_be_reassigned_to_another_execution(root_turn):
    await freeze(root_turn)
    deps, prepared, grants, intent, origin, scope, binding, transport = root_turn
    original, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    from mux.errors import OperationConflict

    async with turn_origin(
        deps.sessionmaker,
        tenant_id=prepared.admission.grant.tenant_id,
        account_id=prepared.admission.account_id,
        platform="discord",
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name="root",
        role=Role.USER,
    ) as other:
        grants[0] = replace(grants[0], fleet=replace(grants[0].fleet, origin_id=other.id))
        with pytest.raises(OperationConflict):
            await freeze(root_turn)
    retained, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    assert retained == original and len(transport.requests) == 2


async def test_one_native_origin_cannot_create_a_second_root_budget(root_turn):
    await freeze(root_turn)
    deps, prepared, grants, intent, origin, scope, binding, transport = root_turn
    original, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    async with deps.sessionmaker.begin() as db:
        other = await turn_card_intents.create_turn_card_intent(
            db,
            tenant_id=intent.tenant_id,
            platform=intent.platform,
            channel_id=intent.channel_id,
            thread_id=intent.thread_id,
            turn_token=uuid4(),
        )
    with pytest.raises(OperationConflict):
        await freeze((deps, prepared, grants, other, origin, scope, binding, transport))
    retained, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    assert retained.fleet.run_id == original.fleet.run_id and len(transport.requests) == 2


@pytest.mark.parametrize("origin_kind", ("worker", "chat"))
async def test_installed_loader_does_not_capture_worker_or_ordinary_turn(root_turn, origin_kind):
    deps, prepared, _, intent, origin, scope, _, transport = root_turn
    prepared = replace(prepared, _source=None)

    async def forbidden(grant, source):
        raise AssertionError("worker cannot load root grants")

    deps = replace(deps, fleet_root_grants=forbidden)

    async def reseed():
        return "worker message"

    lifecycle = RecordingLifecycle()
    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=prepared.admission.grant.tenant_id,
        platform="discord",
        thread_id=origin.thread_id,
        external_user_id="requester",
        user_message="worker message",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: lifecycle,
        origin=origin_kind,
    )
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
    assert any(r.method == "POST" and r.path.endswith("/events") for r in transport.requests)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))


async def new_intent(value):
    deps, _, _, intent, _, _, _, _ = value
    async with deps.sessionmaker.begin() as db:
        return await turn_card_intents.create_turn_card_intent(
            db,
            tenant_id=intent.tenant_id,
            platform=intent.platform,
            channel_id=intent.channel_id,
            thread_id=intent.thread_id,
            turn_token=uuid4(),
        )


async def assert_one_run(value):
    deps, _, _, intent, _, _, _, transport = value
    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(FleetRun)
                .where(FleetRun.tenant_id == intent.tenant_id)
            )
            == 1
        )
    assert len(transport.requests) == 2


async def test_pending_origin_reservation_refuses_a_second_intent(root_turn):
    deps, prepared, grants, intent, origin, scope, binding, transport = root_turn
    async with deps.sessionmaker.begin() as db:
        reserved = await mux_state.get_fleet_root_operation(
            db,
            scope,
            intent_id=intent.id,
            origin_id=origin.id,
            request_digest="reserved",
            slot=binding_slot(binding),
        )
    assert reserved.operation.status == "pending"
    other = await new_intent(root_turn)
    with pytest.raises(OperationConflict):
        await freeze((deps, prepared, grants, other, origin, scope, binding, transport))
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    assert len(transport.requests) == 2


async def test_committed_run_without_receipt_cannot_allocate_again(root_turn, monkeypatch):
    deps, prepared, grants, intent, origin, scope, binding, transport = root_turn
    original = fleet.create_run

    async def lost_receipt(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(fleet, "create_run", lost_receipt)
    with pytest.raises(asyncio.CancelledError):
        await freeze(root_turn)
    async with deps.sessionmaker() as db:
        reserved = await mux_state.get_fleet_root_operation(db, scope, origin_id=origin.id)
    assert reserved.operation.status == "outcome_unknown"
    assert reserved.result["intent_id"] == str(intent.id)
    other = await new_intent(root_turn)
    with pytest.raises(OperationConflict):
        await freeze((deps, prepared, grants, other, origin, scope, binding, transport))
    with pytest.raises(UncertainSend):
        await freeze(root_turn)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    await assert_one_run(root_turn)


async def test_interleaved_intents_reserve_one_origin_before_run_receipt(root_turn, monkeypatch):
    deps, prepared, _, intent, origin, scope, binding, _ = root_turn
    other = await new_intent(root_turn)
    committed, release = asyncio.Event(), asyncio.Event()
    original = fleet.create_run

    async def delayed_receipt(*args, **kwargs):
        result = await original(*args, **kwargs)
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(fleet, "create_run", delayed_receipt)
    persistence = TurnPersistence(deps.state_store, binding, scope, operation_key="interleaved")

    async def write():
        with persistence.activate():
            first = asyncio.create_task(
                freeze_prepared_root(
                    deps, prepared, intent_id=intent.id, now=lambda: datetime.now(UTC)
                )
            )
            try:
                await asyncio.wait_for(committed.wait(), timeout=10)
                async with deps.sessionmaker() as db:
                    reserved = await mux_state.get_fleet_root_operation(
                        db, scope, origin_id=origin.id
                    )
                assert reserved.operation.status == "sent"
                with pytest.raises(OperationConflict):
                    await freeze_prepared_root(
                        deps, prepared, intent_id=other.id, now=lambda: datetime.now(UTC)
                    )
            finally:
                release.set()
                await first

    await persistence.run(write)
    await assert_one_run(root_turn)
    retained, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=other.id) is None
    assert retained.fleet.origin_id == origin.id


@asynccontextmanager
async def another_account_root(value):
    deps, prepared, grants, intent, _, _, binding, _ = value
    tenant_id = intent.tenant_id
    async with deps.sessionmaker.begin() as db:
        tenant = await get_tenant(db, tenant_id)
        assert tenant is not None
        account = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db,
            tenant=tenant,
            account=account,
            platform="discord",
            external_id="another-requester",
        )
    scope = resource_scope(
        tenant_id=str(tenant_id), account_id=str(account.id), authorization_id="another-turn"
    )
    native = prepared.admission.agent
    snapshot = ma_session(
        id="another-root-" + str(uuid4()),
        agent=native,
        environment_id=prepared.admission.environment.id,
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/root-source",
        lambda request, match: httpx.Response(200, json=native.model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/sessions",
        lambda request, match: httpx.Response(200, json=snapshot.model_dump(mode="json")),
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        source = await load_backend_agent(
            client, deps.sessionmaker, tenant_id=tenant_id, agent_name="root", actual_agent=actual
        )
        source = await project_session_resources(
            source,
            agent=actual,
            environment=prepared.admission.environment,
            session_factory=deps.sessionmaker,
            tenant_id=tenant_id,
            agent_uuid=source.agent_id,
            account_id=account.id,
            resource_scope=scope,
            memory_read_only=True,
        )
        acknowledged = await create_session_record(
            client,
            scope=scope,
            agent=actual.id,
            environment_id=prepared.admission.environment.id,
            metadata=None,
            resources=None,
        )
        grant = replace(
            prepared.admission.grant, subject=Subject(platform_user_id="another-requester")
        )
        admission = replace(prepared.admission, account_id=account.id, agent=actual, grant=grant)
        binding = binding.model_copy(
            update={
                "id": str(uuid4()),
                "legacy_account_id": str(account.id),
                "native_refs": {"session": acknowledged.id, "agent": actual.id},
            }
        )
        await deps.state_store.put_binding(binding, expected_generation=0)
        backend = managed_agents(
            client, scope=scope, resources=frozenset({("session", acknowledged.id)})
        )
        prepared = replace(
            prepared,
            admission=admission,
            ma_session_id=acknowledged.id,
            session_account_id=account.id,
            backend=backend,
            session_ref=resource_ref(backend, "session", acknowledged.id, scope=scope),
            _scope=scope,
            _source=source,
        )
        async with turn_origin(
            deps.sessionmaker,
            tenant_id=tenant_id,
            account_id=account.id,
            platform="discord",
            parent_channel_id=intent.channel_id,
            thread_id=intent.thread_id,
            responder_ma_agent_id=actual.id,
            responder_name="root",
            role=Role.USER,
        ) as origin:
            root = replace(
                grants[0],
                fleet=replace(
                    grants[0].fleet, scope=scope, origin_id=origin.id, account_id=account.id
                ),
                subject=grant.subject,
                source_agents=(source.source.ref,),
                source_revisions=((source.source.ref, source.source.revision),),
            )

            deps = replace(deps, anthropic=client, fleet_root_grants=None)
            yield deps, prepared, [root], intent, origin, scope, binding, transport


@pytest.mark.parametrize("status", ("pending", "sent", "outcome_unknown", "processed"))
async def test_one_intent_cannot_be_reserved_by_another_account(root_turn, status):
    deps, _, _, intent, origin, scope, binding, _ = root_turn
    if status == "processed":
        await freeze(root_turn)
    else:
        async with deps.sessionmaker.begin() as db:
            reserved = await mux_state.get_fleet_root_operation(
                db,
                scope,
                intent_id=intent.id,
                origin_id=origin.id,
                request_digest="original-account",
                slot=binding_slot(binding),
            )
        if status != "pending":
            lease = await deps.state_store.acquire_lease(
                binding_slot(binding),
                holder="original-account",
                turn_id="original-turn",
                now=datetime.now(UTC),
                ttl=timedelta(seconds=30),
            )
            try:
                await deps.state_store.claim_send(
                    scope, reserved.operation.key, now=datetime.now(UTC), fence=lease
                )
                if status == "outcome_unknown":
                    await deps.state_store.advance_operation(
                        scope,
                        reserved.operation.key,
                        status,
                        now=datetime.now(UTC),
                        fence=lease,
                    )
            finally:
                await deps.state_store.release_lease(lease)
    async with deps.sessionmaker() as db:
        original = await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id)
    assert original.operation.status == status
    async with another_account_root(root_turn) as other:
        other_deps, _, _, _, other_origin, other_scope, _, other_transport = other
        with pytest.raises(OperationConflict):
            await freeze(other)
        async with deps.sessionmaker() as db:
            assert (
                await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) == original
            )
            assert (
                await mux_state.get_fleet_root_operation(db, other_scope, intent_id=intent.id)
                is None
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(MuxOperation)
                    .where(
                        MuxOperation.tenant_id == intent.tenant_id,
                        MuxOperation.key.startswith("fleet_root:"),
                    )
                )
                == 1
            )
            assert await db.scalar(
                select(func.count())
                .select_from(FleetRun)
                .where(FleetRun.tenant_id == intent.tenant_id)
            ) == int(status == "processed")
        with pytest.raises(WorkerSpecRefused):
            await read_frozen_root(
                other_deps, other_scope, intent_id=intent.id, now=datetime.now(UTC)
            )
        assert len(other_transport.requests) == 2
        distinct = await new_intent(other)
        await freeze((*other[:3], distinct, *other[4:]))
        retained, _ = await read_frozen_root(
            other_deps, other_scope, intent_id=distinct.id, now=datetime.now(UTC)
        )
        assert retained.fleet.origin_id == other_origin.id
        assert retained.fleet.scope == other_scope
        async with deps.sessionmaker() as db:
            assert (
                await mux_state.get_fleet_root_operation(db, other_scope, intent_id=intent.id)
                is None
            )
            assert (
                await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) == original
            )
        assert len(other_transport.requests) == 2


async def run_root_input(value, *, intent_id=None, completion=None):
    deps, prepared, _, intent, origin, _, _, _ = value

    async def reseed():
        return "root input"

    lifecycle = RecordingLifecycle()
    return await run_prepared_turn(
        deps,
        prepared,
        tenant_id=prepared.admission.grant.tenant_id,
        platform="discord",
        thread_id=origin.thread_id,
        external_user_id="requester",
        user_message="root input",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: lifecycle,
        turn_card_intent_id=intent_id or intent.id,
        worker_finished=completion,
    )


async def completion_claim(value, *, ordinal=0, reviewer_of=None):
    from daimon.core.continuity.continuation import ContinuationRequest
    from daimon.core.continuity.wakes import (
        arm_worker_finished,
        claim_wake,
        start_wake,
        worker_finished_key,
    )
    from daimon.core.stores.task_continuations import get_continuation

    deps, prepared, _, intent, origin, scope, _, _ = value
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=origin.tenant_id, run_id=parent.fleet.run_id
    )
    worker = await fleet.add_worker(
        deps.sessionmaker,
        tenant_id=run.tenant_id,
        run_id=run.id,
        ordinal=ordinal,
        role="review" if reviewer_of else "research",
        spec={},
        spec_digest=f"member-{ordinal}",
        backend="anthropic",
        profile="anthropic.managed_agents",
        model="claude-haiku-5-5",
        budget_usd=Decimal(0),
        deadline_at=run.deadline_at,
        max_turns=1,
        reviewer_of=reviewer_of,
    )
    worker = await fleet.finish(
        deps.sessionmaker,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=worker.version,
        status="cancelled",
        error_code="cancelled",
    )
    now = datetime.now(UTC)
    request = ContinuationRequest(
        tenant_id=run.tenant_id,
        platform="discord",
        parent_channel_id=run.channel_id,
        thread_id=run.thread_id,
        requester_account_id=run.orchestrator_account_id,
        requester_external_user_id="requester",
        target_ma_agent_id=prepared.admission.agent.id,
        target_name=prepared.admission.agent.name,
        requested_work=None,
        reason="worker_finished",
        idempotency_key=worker_finished_key(run.id, [worker.id]),
    )
    async with deps.sessionmaker.begin() as db:
        row = await arm_worker_finished(db, request, run_id=run.id, worker_ids=[worker.id], now=now)
    assert row is not None
    claimed = await claim_wake(deps.sessionmaker, idempotency_key=row.idempotency_key, now=now)
    assert claimed is not None
    assert await start_wake(deps.sessionmaker, claimed, now=now)
    async with deps.sessionmaker() as db:
        row = await get_continuation(db, idempotency_key=row.idempotency_key)
    assert row is not None and row.started_at is not None
    return row, worker


@asynccontextmanager
async def wake_frame(value):
    deps, prepared, _, _, origin, _, _, _ = value
    async with deps.sessionmaker.begin() as db:
        intent = await turn_card_intents.create_turn_card_intent(
            db,
            tenant_id=origin.tenant_id,
            platform="discord",
            channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            turn_token=uuid4(),
        )
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=origin.tenant_id,
        account_id=origin.account_id,
        platform="discord",
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
    ) as current:
        yield intent, current


async def test_started_completion_carries_original_run_across_two_real_turn_frames(root_turn):
    deps, prepared, grants, intent, seed, scope, _, transport = root_turn
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        keep_fleet_root=True,
    ) as origin:
        grants[0] = replace(grants[0], fleet=replace(grants[0].fleet, origin_id=origin.id))
        root_turn = (*root_turn[:4], origin, *root_turn[5:])
        await run_root_input(root_turn)
        original, _ = await read_frozen_root(
            deps, scope, origin_id=origin.id, now=datetime.now(UTC)
        )
    async with deps.sessionmaker() as db:
        retained = await turn_origins.get_active_origin(
            db,
            origin_id=origin.id,
            tenant_id=origin.tenant_id,
            account_id=origin.account_id,
            platform="discord",
            now=datetime.now(UTC),
        )
    assert retained is not None and retained.expires_at == original.limits.deadline
    async with deps.sessionmaker.begin() as db:
        assert not await turn_card_intents.retire_turn_card_intent(
            db,
            id=intent.id,
            expected_message_id=None,
            keep_fleet_root=True,
        )

    async def no_new_authority(_grant, _source):
        raise AssertionError("A completion must not reload grants")

    value = (replace(deps, fleet_root_grants=no_new_authority), *root_turn[1:])
    previous = None
    for ordinal in (0, 1):
        row, worker = await completion_claim(value, ordinal=ordinal, reviewer_of=previous)
        async with wake_frame(value) as (current_intent, current_origin):
            await run_root_input(value, intent_id=current_intent.id, completion=row)
            carried, _ = await read_frozen_root(
                deps,
                scope,
                origin_id=current_origin.id,
                run_id=original.fleet.run_id,
                now=datetime.now(UTC),
            )
            assert carried == original
        by_card, _ = await read_frozen_root(
            deps,
            scope,
            intent_id=current_intent.id,
            run_id=original.fleet.run_id,
            now=datetime.now(UTC),
        )
        assert by_card == original
        async with deps.sessionmaker() as db:
            assert (
                await turn_origins.get_active_origin(
                    db,
                    origin_id=current_origin.id,
                    tenant_id=origin.tenant_id,
                    account_id=origin.account_id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
                is None
            )
        previous = worker.id
    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(FleetRun)
                .where(FleetRun.tenant_id == origin.tenant_id)
            )
            == 1
        )
    assert sum(r.method == "POST" and r.path.endswith("/events") for r in transport.requests) == 3
    assert sum(r.method == "POST" and r.path == "/v1/sessions" for r in transport.requests) == 1
    async with deps.sessionmaker() as db:
        records = (
            await db.scalars(select(MuxOperation).where(MuxOperation.tenant_id == origin.tenant_id))
        ).all()
    locators = [r.record["result"] for r in records if "root_origin_id" in r.record["result"]]
    assert len(locators) == 2 and all("permissions" not in r for r in locators)
    assert all(r["run_id"] == str(original.fleet.run_id) for r in locators)


@pytest.mark.parametrize(
    "change", ["unstarted", "owner", "attempt", "start", "expired", "account", "tenant", "session"]
)
async def test_completion_claim_refuses_before_input_or_root_reservation(root_turn, change):
    from daimon.core._models import TaskContinuation
    from sqlalchemy import update

    deps, prepared, _, _, _, scope, binding, transport = root_turn
    await run_root_input(root_turn)
    row, _ = await completion_claim(root_turn)
    value = root_turn
    if change == "unstarted":
        row = row.model_copy(update={"started_at": None})
    elif change == "owner":
        row = row.model_copy(update={"lease_owner": "different-claim"})
    elif change == "attempt":
        row = row.model_copy(update={"attempts": row.attempts + 1})
    elif change == "start":
        row = row.model_copy(update={"started_at": row.started_at + timedelta(seconds=1)})
    elif change == "expired":
        async with deps.sessionmaker.begin() as db:
            await db.execute(
                update(TaskContinuation)
                .where(TaskContinuation.id == row.id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    elif change == "account":
        row = row.model_copy(update={"requester_account_id": uuid4()})
    elif change == "tenant":
        row = row.model_copy(update={"tenant_id": uuid4()})
    else:
        value = (deps, replace(prepared, ma_session_id="another-session"), *root_turn[2:])
    before = sum(r.method == "POST" for r in transport.requests)
    async with wake_frame(value) as (intent, origin):
        from daimon.core.continuity.wakes import WorkerFinishedRootChanged
        from daimon.core.turn.runtime_manifest import resume_prepared_root

        persistence = TurnPersistence(
            deps.state_store, binding, scope, operation_key=f"wake:{row.id}"
        )

        async def attempt():
            with persistence.activate():
                await resume_prepared_root(
                    deps, value[1], row, intent_id=intent.id, now=lambda: datetime.now(UTC)
                )

        expected = (
            WorkerSpecRefused
            if change in {"unstarted", "account", "tenant"}
            else WorkerFinishedRootChanged
        )
        with pytest.raises(expected):
            await persistence.run(attempt)
        async with deps.sessionmaker() as db:
            assert await mux_state.get_fleet_root_operation(db, scope, origin_id=origin.id) is None
    assert sum(r.method == "POST" for r in transport.requests) == before
    async with deps.sessionmaker() as db:
        assert await db.scalar(
            select(func.count()).select_from(FleetRun).where(FleetRun.tenant_id == row.tenant_id)
        ) == (0 if change == "tenant" else 1)


@pytest.mark.parametrize("status", ["running", "collecting", "cancelled", "failed", "completed"])
async def test_retained_root_readers_follow_later_run_transitions(root_turn, status):
    deps, prepared, grants, intent, seed, scope, _, transport = root_turn
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        keep_fleet_root=True,
    ) as origin:
        grants[0] = replace(grants[0], fleet=replace(grants[0].fleet, origin_id=origin.id))
        value = (*root_turn[:4], origin, *root_turn[5:])
        await run_root_input(value)
        original, _ = await read_frozen_root(
            deps, scope, intent_id=intent.id, now=datetime.now(UTC)
        )
    # End the run only AFTER the original frame has retained its live context.
    async with deps.sessionmaker.begin() as db:
        assert not await turn_card_intents.retire_turn_card_intent(
            db, id=intent.id, expected_message_id=None, keep_fleet_root=True
        )
    row, _ = await completion_claim(value)
    async with wake_frame(value) as (current_intent, current):
        await run_root_input(value, intent_id=current_intent.id, completion=row)
        run, _ = await fleet.run_status(
            deps.sessionmaker, tenant_id=origin.tenant_id, run_id=original.fleet.run_id
        )
        if status == "completed":
            run = await fleet.set_run_status(
                deps.sessionmaker,
                tenant_id=run.tenant_id,
                run_id=run.id,
                expected_version=run.version,
                status="collecting",
            )
        if status != "running":
            await fleet.set_run_status(
                deps.sessionmaker,
                tenant_id=run.tenant_id,
                run_id=run.id,
                expected_version=run.version,
                status=status,
            )
        async with deps.sessionmaker() as db:
            assert (
                await turn_origins.get_active_origin(
                    db,
                    origin_id=origin.id,
                    tenant_id=origin.tenant_id,
                    account_id=origin.account_id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
                is not None
            )
            card = await turn_card_intents.get_turn_card_intent(db, id=intent.id)
            assert card is not None and card.status in ("prepared", "posted")
        requests = tuple(transport.requests)
        for selector in (
            {"origin_id": origin.id},
            {"intent_id": intent.id},
            {"origin_id": current.id},
            {"intent_id": current_intent.id},
        ):
            if status in ("running", "collecting"):
                parent, _ = await read_frozen_root(deps, scope, **selector, now=datetime.now(UTC))
                assert parent == original
            else:
                with pytest.raises(WorkerSpecRefused, match="stale"):
                    await read_frozen_root(deps, scope, **selector, now=datetime.now(UTC))
        assert tuple(transport.requests) == requests


@pytest.mark.parametrize("terminal", ["cancelled", "failed", "completed", "expired"])
async def test_original_context_and_card_are_not_retained_for_a_closed_run(root_turn, terminal):
    from sqlalchemy import update

    deps, prepared, grants, intent, seed, scope, _, _ = root_turn
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        keep_fleet_root=True,
    ) as origin:
        grants[0] = replace(grants[0], fleet=replace(grants[0].fleet, origin_id=origin.id))
        value = (*root_turn[:4], origin, *root_turn[5:])
        await run_root_input(value)
        parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
        run, _ = await fleet.run_status(
            deps.sessionmaker, tenant_id=origin.tenant_id, run_id=parent.fleet.run_id
        )
        if terminal == "expired":
            async with deps.sessionmaker.begin() as db:
                await db.execute(
                    update(FleetRun)
                    .where(FleetRun.id == run.id)
                    .values(deadline_at=datetime.now(UTC))
                )
        else:
            if terminal == "completed":
                run = await fleet.set_run_status(
                    deps.sessionmaker,
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    expected_version=run.version,
                    status="collecting",
                )
            await fleet.set_run_status(
                deps.sessionmaker,
                tenant_id=run.tenant_id,
                run_id=run.id,
                expected_version=run.version,
                status=terminal,
            )
        async with deps.sessionmaker.begin() as db:
            assert await turn_card_intents.retire_turn_card_intent(
                db, id=intent.id, expected_message_id=None, keep_fleet_root=True
            )
    async with deps.sessionmaker() as db:
        assert (
            await turn_origins.get_active_origin(
                db,
                origin_id=origin.id,
                tenant_id=origin.tenant_id,
                account_id=origin.account_id,
                platform="discord",
                now=datetime.now(UTC),
            )
            is None
        )
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(
            deps, scope, origin_id=origin.id, run_id=run.id, now=datetime.now(UTC)
        )


@pytest.mark.parametrize(
    "fault",
    [
        "pending",
        "outcome_unknown",
        "cycle",
        "run",
        "binding",
        "scope",
        "retired_intent",
        "original_deleted",
        "requested_run",
    ],
)
async def test_resume_reader_refuses_uncertain_or_conflicting_locators(root_turn, fault):
    import copy

    from sqlalchemy import update

    deps, _, _, _, original_origin, scope, _, transport = root_turn
    await run_root_input(root_turn)
    row, _ = await completion_claim(root_turn)
    async with wake_frame(root_turn) as (intent, current):
        await run_root_input(root_turn, intent_id=intent.id, completion=row)
        retained, _ = await read_frozen_root(
            deps, scope, origin_id=current.id, now=datetime.now(UTC)
        )
        async with deps.sessionmaker.begin() as db:
            key = f"fleet_root:{current.id}:root:0"
            operation = await db.scalar(
                select(MuxOperation).where(
                    MuxOperation.tenant_id == current.tenant_id, MuxOperation.key == key
                )
            )
            body = copy.deepcopy(operation.record)
            if fault in {"pending", "outcome_unknown"}:
                body["operation"]["status"] = fault
                await db.execute(
                    update(MuxOperation)
                    .where(
                        (MuxOperation.tenant_id == operation.tenant_id)
                        & (MuxOperation.account_id == operation.account_id)
                        & (MuxOperation.key == operation.key)
                    )
                    .values(status=fault, record=body)
                )
            elif fault == "retired_intent":
                assert await turn_card_intents.retire_turn_card_intent(
                    db, id=intent.id, expected_message_id=None
                )
            elif fault == "original_deleted":
                await turn_origins.delete_origin(db, origin_id=original_origin.id)
            elif fault != "requested_run":
                result = body["result"]
                if fault == "cycle":
                    result["root_origin_id"] = str(current.id)
                elif fault == "run":
                    result["run_id"] = str(uuid4())
                elif fault == "binding":
                    result["binding"]["generation"] += 1
                else:
                    result["scope"]["account_id"] = str(uuid4())
                await db.execute(
                    update(MuxOperation)
                    .where(
                        (MuxOperation.tenant_id == operation.tenant_id)
                        & (MuxOperation.account_id == operation.account_id)
                        & (MuxOperation.key == operation.key)
                    )
                    .values(record=body)
                )
        before = len(transport.requests)
        with pytest.raises((WorkerSpecRefused, ScopeViolation)):
            await read_frozen_root(
                deps,
                scope,
                origin_id=current.id,
                run_id=uuid4() if fault == "requested_run" else retained.fleet.run_id,
                now=datetime.now(UTC),
            )
        assert len(transport.requests) == before


async def test_original_run_selected_even_with_a_newer_root_on_the_same_binding(root_turn):
    deps, prepared, grants, intent, origin, scope, _, _ = root_turn
    await run_root_input(root_turn)
    original, _ = await read_frozen_root(deps, scope, origin_id=origin.id, now=datetime.now(UTC))
    row, _ = await completion_claim(root_turn)
    async with wake_frame(root_turn) as (new_intent, new_origin):
        grants[0] = replace(grants[0], fleet=replace(grants[0].fleet, origin_id=new_origin.id))
        await run_root_input(root_turn, intent_id=new_intent.id)
        latest, _ = await read_frozen_root(
            deps, scope, origin_id=new_origin.id, now=datetime.now(UTC)
        )
        assert latest.fleet.run_id != original.fleet.run_id

        async def no_copy(*args):
            raise AssertionError("continuation may not load fresh authority")

        value = (replace(deps, fleet_root_grants=no_copy), *root_turn[1:])
        async with wake_frame(value) as (continuation_intent, continuation_origin):
            await run_root_input(value, intent_id=continuation_intent.id, completion=row)
            carried, _ = await read_frozen_root(
                deps,
                scope,
                origin_id=continuation_origin.id,
                run_id=original.fleet.run_id,
                now=datetime.now(UTC),
            )
            assert carried == original


async def test_retention_never_extends_existing_database_expiry(root_turn):
    from daimon.core._models import TurnOrigin
    from sqlalchemy import update

    deps, _, _, _, origin, _, _, _ = root_turn
    await run_root_input(root_turn)
    earlier = datetime.now(UTC) + timedelta(seconds=30)
    async with deps.sessionmaker.begin() as db:
        await db.execute(
            update(TurnOrigin).where(TurnOrigin.id == origin.id).values(expires_at=earlier)
        )
        await turn_origins.delete_origin(
            db, origin_id=origin.id, retain_until=earlier + timedelta(hours=1)
        )
    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(select(TurnOrigin.expires_at).where(TurnOrigin.id == origin.id))
            == earlier
        )
