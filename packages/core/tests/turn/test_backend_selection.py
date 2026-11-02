"""Actual normal admission keeps authorized native bindings after channel edits."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from daimon.core import channel_backend
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
from daimon.core.stores import mux_state
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_host import OpenAIHostRuntime, prepare_openai
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.prepare import ProviderPreparationRequest, bind_session
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_tenant_config,
)
from daimon.testing.turn_router import build_turn_router
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ResourceRef, Revision, ThreadRef
from mux.contracts.resources import SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.errors import ScopeViolation
from mux.state.lease import Slot
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma import json_body, make_fake_memory_store_handler
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.isolated_sdk import build_async_anthropic, build_async_openai
from structlog.testing import capture_logs

from .test_openai_host import SESSION_ID, Wire
from .test_provider_dispatch import (
    ACCOUNT,
    PROFILE,
    REVISION,
    SCOPE,
    TENANT,
    admission,
    deps_for_pinned_mux,
)


@pytest.fixture
def initial_backend(request):
    return getattr(request, "param", "openai")


def preference_for(host):
    service = channel_backend.AgentBackendService(host.sessions, enabled=True)
    agent = channel_backend.BackendAgent(
        tenant_id=TENANT,
        agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id="ag_parity_test"),
        name="test-agent",
        configuration_digest="actual-offline-agent",
        model="claude-sonnet-4-6",
    )
    actor = channel_backend.BackendActor(TENANT, ACCOUNT, Subject(is_admin=True))
    return service, agent, actor


@pytest.fixture(params=["memory", "postgres"])
async def selector_host(db_preflight, db_session_factory, db_clean, request, initial_backend):
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError:
        pytest.skip("native selector proofs require an owned lane DB or isolated CI")
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, id=TENANT, platform="slack")
        account = await make_account(db, tenant=tenant, id=ACCOUNT)
        await make_platform_principal(
            db, platform="slack", external_id="caller", tenant=tenant, account=account
        )
        await make_tenant_config(
            db, tenant=tenant, agent_name="test-agent", environment_name="test-env"
        )
        await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal(100))
        await set_access_policy(
            db,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini")),
        )
        if initial_backend is not None:
            await channel_backend.set_channel_backend(
                db,
                REVISION.channel,
                BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"),
            )
    store = (
        MemoryStateStore() if request.param == "memory" else PostgresStateStore(db_session_factory)
    )
    if initial_backend is not None:
        await store.put_config_revision(REVISION)
    wire = Wire()
    wire.expected_spend_limit = None
    ancillary = build_turn_router(str(TENANT))
    memory = make_fake_memory_store_handler()
    calls, created = [], []
    handover_agents = {}

    def create(req, match):
        body = json_body(req)
        id_ = f"ancillary-session-{len(created) + 1}"
        created.append(id_)
        result = ma_session(
            id=id_,
            agent_id=body["agent"] if isinstance(body["agent"], str) else body["agent"]["id"],
            environment_id="env_parity_test",
            metadata=body.get("metadata"),
            resources=body.get("resources", ()),
        )
        ancillary.add_session(result, with_archive=True)
        return httpx.Response(200, json=result.model_dump(mode="json"))

    ancillary.add("POST", r"/v1/sessions", create)

    def handle(req):
        calls.append(req)
        target = handover_agents.get(req.url.path.rsplit("/", 1)[-1])
        if req.method == "GET" and req.url.path.startswith("/v1/agents/") and target is not None:
            return httpx.Response(200, json=target.model_dump(mode="json"))
        return (
            memory(req) if req.url.path.startswith("/v1/memory_stores") else ancillary.dispatch(req)
        )

    async def plan(request):
        return SessionSpec(
            agent=ResourceRef(
                id="native-agent",
                kind="agent",
                provider="openai",
                account_scope_id="project",
                tenant_id=SCOPE.tenant_id,
                account_id=SCOPE.account_id,
            ),
            agent_revision=Revision(local=0),
            config_revision=request.admission.backend_revision.local,
        )

    async with (
        build_async_openai(
            transport=httpx.MockTransport(wire.handle),
            api_key="offline",
            base_url="https://openai.invalid/v1",
        ) as sdk,
        build_async_anthropic(
            transport=httpx.MockTransport(handle),
            api_key="offline",
            base_url="https://anthropic.invalid",
        ) as client,
    ):

        async def read_counterpart(selected):
            from daimon.core.stores.neutral_catalog import AgentRevision
            from mux.contracts.resources import Agent

            return AgentRevision(
                catalog_id="handover-counterpart",
                local_revision=1,
                principal_id="host",
                agent=Agent(
                    ref=ResourceRef(
                        id="native-agent",
                        kind="agent",
                        provider="openai",
                        account_scope_id="project",
                        tenant_id=selected.scope.tenant_id,
                        account_id=selected.scope.account_id,
                    ),
                    revision=Revision(local=1),
                    created_at=datetime.now(UTC),
                    spec=selected.target_spec,
                ),
            )

        runtime = OpenAIHostRuntime(
            lambda revision, scope: SDKTransport(sdk),
            OpenAIRecoveryJournal(),
            OpenAIUsageRevisions(),
            read_agent_counterpart=read_counterpart,
            account_scope_id="project",
            session_plan=plan,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            controls=SessionControls(
                model="gpt-6-luna", multi_agent_enabled=False, container_size="small"
            ),
        )
        deps = replace(
            deps_for_pinned_mux(client, db_session_factory),
            channel_backends=True,
            state_store=store,
            turn_runtimes={PROFILE: runtime},
        )
        native = (
            await prepare_openai(
                ProviderPreparationRequest(
                    deps,
                    admission(),
                    SCOPE,
                    TENANT,
                    "slack",
                    "caller",
                    "thread",
                    ACCOUNT,
                    True,
                    DEFAULT_MA_CAPABILITIES,
                    None,
                    datetime.now(UTC) + timedelta(seconds=30),
                    lambda: datetime.now(UTC),
                )
            )
            if initial_backend is not None
            else None
        )
        admitted = []

        async def admit_thread(thread, **options):
            current = await admit_impl(
                deps,
                origin="chat",
                tenant_id=TENANT,
                requester_org=RequesterOrg.HOST,
                platform="slack",
                external_user_id="caller",
                channel_id="channel",
                thread_id=thread,
                now=datetime.now(UTC),
                **options,
            )
            admitted.append(current)
            return current

        async def prepare(thread, **options):
            current = await admit_thread(thread)
            return await bind_session(
                deps,
                current,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                thread_id=thread,
                session_account_id=ACCOUNT,
                reuse_existing=True,
                **options,
            )

        async def change(config=None):
            async with db_session_factory.begin() as db:
                return await channel_backend.set_channel_backend(
                    db, REVISION.channel, config or BackendConfig()
                )

        slot = Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"), account_id=str(ACCOUNT)
        )
        yield SimpleNamespace(
            deps=deps,
            store=store,
            wire=wire,
            calls=calls,
            created=created,
            native=native,
            admitted=admitted,
            handover_agents=handover_agents,
            prepare=prepare,
            admit=admit_thread,
            change=change,
            sessions=db_session_factory,
            slot=slot,
        )


async def test_authorized_existing_binding_retains_revision_and_new_thread_uses_current(
    selector_host,
):
    host = selector_host
    first = await host.prepare("thread")
    binding = await host.store.get_binding(host.slot)
    assert binding is not None
    current = await host.change()
    existing = await host.prepare("thread")
    new = await host.prepare("new-thread")
    assert first.session_ref == existing.session_ref == host.native.session_ref
    assert existing.ma_session_id == SESSION_ID and existing.reused
    assert existing.admission.backend_revision == REVISION
    assert await host.store.get_binding(host.slot) == binding
    assert new.admission.backend_revision == current and current.local == 2
    assert new.admission.backend_revision.backend == "anthropic"
    assert new.session_ref is None  # Preserve the existing Anthropic prepared shape.
    assert new.ma_session_id != existing.ma_session_id
    assert [item.backend_revision for item in host.admitted] == [REVISION, REVISION, current]
    assert len([req for req in host.wire.requests if req.method == "POST"]) == 1
    assert host.created == [new.ma_session_id]
    assert all(req.url.host == "openai.invalid" for req in host.wire.requests)
    assert all(req.url.host == "anthropic.invalid" for req in host.calls)


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_preparation_pins_the_default_before_any_turn_or_backend_change(selector_host):
    host = selector_host
    first = await host.prepare("thread")
    binding = await host.store.get_binding(host.slot)
    assert binding is not None
    assert binding.config_revision == 0
    assert binding.profile == "anthropic.managed_agents"
    assert binding.native_refs["session"] == first.ma_session_id
    assert first.admission.backend_revision is None
    assert first.admission.backend_footer_profile is None
    assert host.wire.requests == []
    assert not any(req.url.path.endswith("/events") for req in host.calls)

    latest = await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    again = await host.prepare("thread")
    fresh = await host.prepare("new-thread")
    assert again.ma_session_id == first.ma_session_id and again.reused
    assert again.session_ref is None and again.admission.backend_revision is None
    assert again.admission.backend_footer_profile == "anthropic.managed_agents"
    assert await host.store.get_binding(host.slot) == binding
    assert fresh.admission.backend_revision == latest
    assert fresh.session_ref is not None and fresh.session_ref.provider == "openai"
    assert fresh.admission.backend_footer_profile == PROFILE
    assert len([req for req in host.wire.requests if req.method == "POST"]) == 1
    assert host.created == [first.ma_session_id]


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_default_binding_ignores_an_ineligible_new_setting_but_new_thread_refuses(
    selector_host, monkeypatch
):
    host = selector_host
    first = await host.prepare("thread")
    await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    monkeypatch.setattr(
        channel_backend, "RUNNABLE_PROFILES", channel_backend.RUNNABLE_PROFILES - {PROFILE}
    )
    before = (len(host.wire.requests), len(host.calls))
    retained = await host.admit("thread")
    assert retained.backend_revision is None
    assert retained.backend_footer_profile == "anthropic.managed_agents"
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.admit("new-thread")
    assert (len(host.wire.requests), len(host.calls)) == before
    binding = await host.store.get_binding(host.slot)
    assert binding is not None and binding.native_refs["session"] == first.ma_session_id


async def test_removed_provider_allowlist_refuses_an_existing_native_binding(selector_host):
    host = selector_host
    binding = await host.store.get_binding(host.slot)
    async with host.sessions.begin() as db:
        await set_access_policy(db, tenant_id=TENANT, policy=TenantAccessPolicy())
    before = (len(host.wire.requests), len(host.calls))
    with capture_logs() as logs, pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.prepare("thread")
    assert any(row.get("stage") == "workspace_policy" for row in logs)
    assert any(row.get("stage") == "channel_policy" for row in logs)
    assert (len(host.wire.requests), len(host.calls)) == before
    assert await host.store.get_binding(host.slot) == binding


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_agent_preference_and_revert_select_new_threads_without_channel_override(
    selector_host,
):
    host = selector_host
    original = await host.prepare("thread")
    original_binding = await host.store.get_binding(host.slot)
    async with host.sessions() as db:
        previous_revision, _ = await mux_state.latest_config_revision_and_sharing(
            db, REVISION.channel
        )
    service, agent, actor = preference_for(host)
    changed = await service.set_backend(agent, "openai", 0, actor)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.admit("new-thread")
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.prepare("new-thread")
    async with host.sessions() as db:
        # The pre-origin image's reader has no origin filter. A refused turn
        # must leave no row that it could interpret as a channel override.
        legacy_revision, _ = await mux_state.latest_config_revision_and_sharing(
            db, REVISION.channel
        )
        assert legacy_revision == previous_revision
    old = await host.prepare("thread")
    assert old.ma_session_id == original.ma_session_id and old.admission.backend_revision is None
    assert old.admission.backend_footer_profile == "anthropic.managed_agents"
    assert await host.store.get_binding(host.slot) == original_binding
    # M03: the thread tried to follow its agent to OpenAI, which can't serve it here;
    # that move was withdrawn, so nothing is left to move it on a later message.
    from daimon.core.stores import thread_provider_releases

    async with host.sessions() as db:
        assert await thread_provider_releases.requested_generation(db, host.slot) is None
    restored = await service.revert(agent, actor, expected_revision=changed.local)
    assert restored.backend is None and restored.previous_local == changed.local
    new_default = await host.prepare("after-revert")
    assert new_default.admission.backend_revision is None and new_default.session_ref is None
    assert host.wire.requests == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_preexisting_legacy_mapping_retains_default_when_the_feature_is_enabled(
    selector_host,
):
    host = selector_host
    admitted = await host.admit("thread")
    old = await bind_session(
        replace(host.deps, channel_backends=False),
        admitted,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id="thread",
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )
    assert await host.store.get_binding(host.slot) is None
    await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    retained = await host.prepare("thread")
    assert retained.admission.backend_revision is None
    assert retained.ma_session_id == old.ma_session_id and retained.reused
    published = await host.store.get_binding(host.slot)
    assert published is not None and published.config_revision == 0
    assert published.native_refs["session"] == old.ma_session_id


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("defect", ["provider", "profile", "registration"])
async def test_invalid_default_pin_refuses_before_selecting_a_new_provider(
    selector_host, monkeypatch, defect
):
    host = selector_host
    await host.prepare("thread")
    await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    binding = await host.store.get_binding(host.slot)
    assert binding is not None
    if defect == "registration":
        monkeypatch.setattr(
            channel_backend,
            "RUNNABLE_PROFILES",
            channel_backend.RUNNABLE_PROFILES - {binding.profile},
        )
    else:
        damaged = binding.model_copy(
            update={"provider": "openai"} if defect == "provider" else {"profile": PROFILE}
        )
        get_binding = host.store.get_binding

        async def corrupted(slot, **options):
            return damaged if slot == host.slot else await get_binding(slot, **options)

        monkeypatch.setattr(host.store, "get_binding", corrupted)
    before = (len(host.wire.requests), len(host.calls))
    with capture_logs() as logs, pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.prepare("thread")
    expected = (
        "retained_default_policy" if defect == "registration" else "retained_default_mismatch"
    )
    assert any(row.get("stage") == expected for row in logs)
    assert (len(host.wire.requests), len(host.calls)) == before


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_unavailable_agent_preference_cannot_block_a_retained_default_thread(
    selector_host, monkeypatch
):
    host = selector_host
    await host.prepare("thread")
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "openai", 0, actor)
    monkeypatch.setattr(
        channel_backend, "RUNNABLE_PROFILES", channel_backend.RUNNABLE_PROFILES - {PROFILE}
    )
    before = (len(host.wire.requests), len(host.calls))
    retained = await host.admit("thread")
    assert retained.backend_revision is None
    assert retained.backend_footer_profile == "anthropic.managed_agents"
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.admit("new-thread")
    assert (len(host.wire.requests), len(host.calls)) == before


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_explicit_channel_override_wins_over_an_unavailable_agent_preference(
    selector_host, monkeypatch
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "openai", 0, actor)
    override = await host.change(BackendConfig())
    monkeypatch.setattr(
        channel_backend, "RUNNABLE_PROFILES", channel_backend.RUNNABLE_PROFILES - {PROFILE}
    )
    prepared = await host.prepare("thread")
    assert prepared.admission.backend_revision == override
    assert prepared.admission.backend_footer_profile == "anthropic.managed_agents"
    assert prepared.session_ref is None
    assert host.wire.requests == []
    binding = await host.store.get_binding(host.slot)
    assert binding is not None and binding.config_revision == override.local


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("target", ["anthropic", "openai", "gemini"])
async def test_a_same_named_resource_cannot_impersonate_the_source_of_a_preference(
    selector_host, target
):
    host = selector_host
    service, agent, actor = preference_for(host)
    namesake = agent.model_copy(update={"agent_id": uuid.uuid4()})
    await service.set_backend(namesake, target, 0, actor)
    selected = await host.admit("new-thread")
    assert selected.backend_revision is None
    assert selected.backend_revision_origin is None
    assert selected.backend_footer_profile is None
    assert host.wire.requests == []
    assert host.created == []
    assert not any(req.url.path.endswith("/events") for req in host.calls)


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("target", ["openai", "gemini"])
async def test_private_dm_does_not_silently_ignore_an_explicit_native_agent_preference(
    selector_host, target
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, target, 0, actor)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.admit("dm-thread", is_dm=True)
    async with host.sessions() as db:
        legacy_revision, _ = await mux_state.latest_config_revision_and_sharing(
            db, REVISION.channel
        )
        assert legacy_revision is None
    assert host.wire.requests == []
    assert host.created == []


async def test_missing_retained_provider_registration_refuses_before_any_provider_effect(
    selector_host, monkeypatch
):
    host = selector_host
    await host.change()
    before = (len(host.wire.requests), len(host.calls))
    monkeypatch.setattr(
        channel_backend, "RUNNABLE_PROFILES", channel_backend.RUNNABLE_PROFILES - {PROFILE}
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.prepare("thread")
    assert (len(host.wire.requests), len(host.calls)) == before
    assert host.created == []


@pytest.mark.parametrize("defect", ["missing", "provider", "profile", "revision", "channel"])
async def test_missing_or_mismatched_retained_revision_refuses_before_provider_effect(
    selector_host, monkeypatch, defect
):
    host = selector_host
    await host.change()
    before = (len(host.wire.requests), len(host.calls))
    retained = REVISION
    if defect == "provider":
        retained = REVISION.model_copy(update={"backend": "anthropic"})
    elif defect == "profile":
        retained = REVISION.model_copy(update={"profile": "gemini.inline_reuse"})
    elif defect == "revision":
        retained = REVISION.model_copy(update={"local": 2})
    elif defect == "channel":
        retained = REVISION.model_copy(
            update={"channel": REVISION.channel.model_copy(update={"channel_id": "foreign"})}
        )

    async def read(db, channel, local):
        assert channel == REVISION.channel and local == 1
        return None if defect == "missing" else retained

    monkeypatch.setattr(mux_state, "get_config_revision", read)
    with capture_logs() as logs, pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.prepare("thread")
    expected = "retained_revision_lookup" if defect == "missing" else "retained_binding_mismatch"
    assert any(row.get("stage") == expected for row in logs)
    assert (len(host.wire.requests), len(host.calls)) == before
    assert host.created == []


@pytest.mark.parametrize("defect", ["account", "thread"])
async def test_foreign_binding_is_refused_before_retained_config_or_provider_io(
    selector_host, monkeypatch, defect
):
    host = selector_host
    binding = await host.store.get_binding(host.slot)
    assert binding is not None
    if defect == "account":
        binding = binding.model_copy(update={"legacy_account_id": "foreign"})
    else:
        binding = binding.model_copy(
            update={"thread": binding.thread.model_copy(update={"thread_id": "foreign"})}
        )
    await host.change()
    before = (len(host.wire.requests), len(host.calls))

    async def read(slot, **_):
        assert slot == host.slot
        return binding

    async def forbidden(*args):
        raise AssertionError("foreign binding reached config read")

    monkeypatch.setattr(host.store, "get_binding", read)
    monkeypatch.setattr(mux_state, "get_config_revision", forbidden)
    with pytest.raises(ScopeViolation, match="authorized thread"):
        await host.prepare("thread")
    assert (len(host.wire.requests), len(host.calls)) == before


@pytest.mark.parametrize("path,enabled", [("legacy", True), ("legacy", False), ("mux", False)])
async def test_legacy_or_unconfigured_admission_does_not_read_native_bindings(
    selector_host, monkeypatch, path, enabled
):
    host = selector_host
    latest = await host.change()

    async def forbidden(*args):
        raise AssertionError("legacy/unconfigured admission read a native binding")

    monkeypatch.setattr(host.store, "get_binding", forbidden)
    monkeypatch.setattr(channel_backend, "load_backend_agent", forbidden)
    monkeypatch.setattr(channel_backend, "current_agent_backend", forbidden)
    monkeypatch.setattr(channel_backend, "resolve_turn_backend", forbidden)
    current = await admit_impl(
        replace(host.deps, turn_path=path, channel_backends=enabled),
        origin="chat",
        tenant_id=TENANT,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id="thread",
        now=datetime.now(UTC),
    )
    assert current.backend_revision == (latest if enabled else None)
    assert host.created == []


async def test_another_caller_never_selects_the_private_native_binding(selector_host):
    host = selector_host
    other = uuid.UUID(int=4410)
    from daimon.core.stores.tenants import get_tenant

    async with host.deps.sessionmaker.begin() as db:
        tenant = await get_tenant(db, TENANT)
        assert tenant is not None
        account = await make_account(db, tenant=tenant, id=other)
        await make_platform_principal(
            db, platform="slack", external_id="other", tenant=tenant, account=account
        )
    latest = await host.change()
    before = len(host.wire.requests)
    current = await admit_impl(
        host.deps,
        origin="chat",
        tenant_id=TENANT,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="other",
        channel_id="channel",
        thread_id="thread",
        now=datetime.now(UTC),
    )
    assert current.account_id == other and current.backend_revision == latest
    prepared = await bind_session(
        host.deps,
        current,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="other",
        thread_id="thread",
        session_account_id=other,
        reuse_existing=True,
    )
    assert prepared.ma_session_id != host.native.ma_session_id
    assert len(host.wire.requests) == before
    assert host.created == [prepared.ma_session_id]
    binding = await host.store.get_binding(host.slot)
    assert binding is not None and binding.native_refs["session"] == host.native.ma_session_id


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_channel_clear_inherits_the_actual_agent_identity_and_keeps_old_channel_pin(
    selector_host,
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "openai", 0, actor)
    explicit = await host.change(BackendConfig())
    original = await host.prepare("thread")
    async with host.sessions.begin() as db:
        cleared = await channel_backend.clear_channel_backend(db, REVISION.channel)
    retained = await host.admit("thread")
    assert retained.backend_revision == explicit
    assert retained.backend_revision_origin == "channel"
    assert retained.backend_footer_profile == "anthropic.managed_agents"
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.admit("new-thread")
    async with host.sessions() as db:
        legacy_revision, _ = await mux_state.latest_config_revision_and_sharing(
            db, REVISION.channel
        )
        assert legacy_revision == cleared
        assert await mux_state.next_config_local(db, REVISION.channel) == cleared.local + 1
    binding = await host.store.get_binding(host.slot)
    assert binding is not None and binding.native_refs["session"] == original.ma_session_id
    assert host.wire.requests == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_agent_selection_projects_the_already_authorized_source_without_an_extra_read(
    selector_host,
    monkeypatch,
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "openai", 0, actor)
    project = channel_backend.load_backend_agent
    projections = []

    async def capture(*args, **kwargs):
        source = kwargs["actual_agent"]
        assert source.id == "ag_parity_test"
        before = len(host.calls)
        resolved = await project(*args, **kwargs)
        assert len(host.calls) == before
        projections.append(resolved.agent_id)
        return resolved

    monkeypatch.setattr(channel_backend, "load_backend_agent", capture)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.admit("new-thread")
    assert projections == [agent.agent_id]
    assert host.wire.requests == []
    assert host.created == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_concrete_handoff_source_uses_its_identity_despite_a_historical_display_name(
    selector_host,
):
    from daimon.core.stores.thread_agent_bindings import create_binding

    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "anthropic", 0, actor)
    async with host.sessions.begin() as db:
        await create_binding(
            db,
            tenant_id=TENANT,
            platform="slack",
            parent_channel_id="channel",
            thread_id="handoff-thread",
            responder_ma_agent_id="ag_parity_test",
            responder_name="historical-display-name",
            kind="handoff",
        )
    selected = await host.admit("handoff-thread")
    assert selected.config.agent_name == "historical-display-name"
    assert selected.agent.id == "ag_parity_test"
    assert selected.backend_revision is not None
    assert selected.backend_revision.backend == "anthropic"
    assert selected.backend_revision_origin == "agent"
    assert host.wire.requests == []
    assert host.created == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("target", ["openai", "gemini"])
async def test_refused_agent_selection_leaves_no_revision_for_a_rollback_reader(
    selector_host, target
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, target, 0, actor)
    async with host.sessions() as db:
        before = await mux_state.latest_config_revision_and_sharing(db, REVISION.channel)
        next_local = await mux_state.next_config_local(db, REVISION.channel)
    for thread in ("new-one", "new-two"):
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            await host.prepare(thread)
    async with host.sessions() as db:
        assert await mux_state.latest_config_revision_and_sharing(db, REVISION.channel) == before
        assert before[0] is None
        assert await mux_state.next_config_local(db, REVISION.channel) == next_local
        assert await channel_backend.current_backend(db, REVISION.channel) is None
    assert host.wire.requests == []
    assert host.created == []
    assert not any(req.url.path.endswith("/events") for req in host.calls)


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_confirmed_release_moves_this_thread_to_the_new_provider_with_a_fresh_session(
    selector_host,
):
    from daimon.core.thread_release import request_thread_release
    from mux.state.store import ERA_REF

    host = selector_host
    first = await host.prepare("thread")
    before = await host.store.get_binding(host.slot)
    assert before is not None and before.provider == "anthropic"
    latest = await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    stays = await host.prepare("thread")
    assert stays.ma_session_id == first.ma_session_id and stays.session_ref is None

    seen = await host.store.get_binding(host.slot, include_released=True)
    async with host.sessions.begin() as db:
        await request_thread_release(
            db, host.slot, generation=seen.generation, now=datetime.now(UTC)
        )
    moved = await host.prepare("thread")
    assert moved.admission.backend_revision == latest
    assert moved.session_ref is not None and moved.session_ref.provider == "openai"
    after = await host.store.get_binding(host.slot)
    assert after is not None and after.provider == "openai"
    # The same slot identity, through the compare-and-swap, never in place.
    assert after.id == before.id and after.generation == before.generation + 2
    assert after.native_refs.get(ERA_REF) == f":g{before.generation + 1}"
    assert after.native_refs["session"] != first.ma_session_id
    again = await host.prepare("thread")
    assert again.session_ref == moved.session_ref and again.reused


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_release_requested_before_the_binding_moved_is_never_applied(selector_host):
    from daimon.core.thread_release import request_thread_release

    host = selector_host
    first = await host.prepare("thread")
    seen = await host.store.get_binding(host.slot, include_released=True)
    async with host.sessions.begin() as db:
        await request_thread_release(
            db, host.slot, generation=seen.generation, now=datetime.now(UTC)
        )
    current = await host.store.get_binding(host.slot)
    # Another turn rebinds the slot after the confirmation saw it.
    moved = current.model_copy(update={"generation": current.generation + 1})
    await host.store.put_binding(moved, expected_generation=current.generation)
    await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    stays = await host.prepare("thread")
    assert stays.ma_session_id == first.ma_session_id and stays.session_ref is None
    assert await host.store.get_binding(host.slot) == moved


async def _request_release(host):
    from daimon.core.thread_release import request_thread_release

    seen = await host.store.get_binding(host.slot, include_released=True)
    async with host.sessions.begin() as db:
        await request_thread_release(
            db, host.slot, generation=seen.generation if seen else 0, now=datetime.now(UTC)
        )
    return seen


async def _apply(host):
    from daimon.core.thread_release import apply_thread_release

    return await apply_thread_release(
        host.sessions,
        host.store,
        host.slot,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        now=datetime.now(UTC),
    )


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_refused_turn_leaves_the_release_for_the_next_one(selector_host, monkeypatch):
    from daimon.core.thread_release import pending_releases
    from daimon.core.turn import admission

    host = selector_host
    await host.prepare("thread")
    seen = await _request_release(host)

    async def refuse(*args, **kwargs):
        raise AdmissionDenied(reason="backend_unsupported")

    # Any refusal after the release is found (access, balance, provider).
    monkeypatch.setattr(admission, "load_access_policy", refuse)
    with pytest.raises(AdmissionDenied):
        await host.prepare("thread")
    assert await host.store.get_binding(host.slot) == seen, "nothing was released"
    async with host.sessions() as db:
        assert len(await pending_releases(db, tenant_id=TENANT)) == 1


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_release_interrupted_after_its_binding_write_is_finished_not_redone(
    selector_host,
):
    from daimon.core.thread_release import pending_releases
    from mux.state.store import released_successor

    host = selector_host
    await host.prepare("thread")
    seen = await _request_release(host)
    # The binding write committed, then the request's transaction failed.
    await host.store.put_binding(released_successor(seen), expected_generation=seen.generation)
    assert await _apply(host) is True
    after = await host.store.get_binding(host.slot, include_released=True)
    assert after.generation == seen.generation + 1
    async with host.sessions() as db:
        assert await pending_releases(db, tenant_id=TENANT) == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("slot_state", ["unbound", "retained", "released"])
async def test_button_resend_uses_normal_provider_retention_and_confirmed_release(
    selector_host, slot_state
):
    from daimon.core.native_admins import AdminCaller
    from daimon.core.stores.task_continuations import list_pending_continuations
    from daimon.core.thread_handoff import switch_thread_on_request
    from daimon.core.thread_release import pending_releases, request_thread_release
    from daimon.testing.ma_models import ma_agent

    host = selector_host
    if slot_state != "unbound":
        await host.prepare("thread")
    before = await host.store.get_binding(host.slot)
    if slot_state == "unbound":
        assert before is None
    else:
        assert before is not None and before.provider == "anthropic"
    target_agent = ma_agent(id="ag_handover_target", name="handover-target", tenant_id=TENANT)
    host.handover_agents[target_agent.id] = target_agent
    from daimon.core.turn.prepare import admitted_session_scope
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    # Configure the destination through today's consent path, including native
    # counterpart readback; a hand-over itself never grants source-capability drops.
    probe = await host.admit("preference-probe")
    probe = replace(
        probe,
        agent=target_agent,
        config=probe.config.model_copy(
            update={"agent_name": target_agent.name, "responder_ma_agent_id": target_agent.id}
        ),
    )
    now = datetime.now(UTC)
    request = ProviderPreparationRequest(
        host.deps,
        probe,
        admitted_session_scope(probe, tenant_id=TENANT, session_id="probe"),
        TENANT,
        "slack",
        "caller",
        "preference-probe",
        ACCOUNT,
        True,
        DEFAULT_MA_CAPABILITIES,
        None,
        now + timedelta(seconds=30),
        lambda: now,
    )
    target = await capture_preparation_source(request, fresh=True)

    async def capture(_agent):
        return await capture_preparation_source(request, fresh=True)

    service = channel_backend.AgentBackendService(
        host.sessions, enabled=True, capture_source=capture
    )
    actor = channel_backend.BackendActor(TENANT, ACCOUNT, Subject(is_admin=True))
    quote = await service.preview(target, "openai")
    await service.set_backend(
        target,
        "openai",
        0,
        actor,
        accepted_drops=quote.declared_drops,
        expected_preview_digest=quote.digest,
    )
    if slot_state == "released":
        async with host.sessions.begin() as db:
            await request_thread_release(
                db, host.slot, generation=before.generation, now=datetime.now(UTC)
            )
    async with host.sessions() as db:
        pending_before = await pending_releases(db, tenant_id=TENANT)
    outcome = await switch_thread_on_request(
        host.deps.anthropic,
        host.sessions,
        tenant_id=TENANT,
        platform="slack",
        parent_channel_id="channel",
        thread_id="thread",
        ma_agent_id=target_agent.id,
        caller=AdminCaller(platform_user_id="caller", is_server_admin=True),
        default=host.deps.deployment_default,
        channel="this channel",
        now=datetime.now(UTC),
    )
    assert outcome.switched
    async with host.sessions() as db:
        assert await pending_releases(db, tenant_id=TENANT) == pending_before
        assert (
            await list_pending_continuations(
                db, tenant_id=TENANT, platform="slack", thread_id="thread"
            )
            == []
        )
    # The adapter integrations exercise this same ordinary admit/bind entry.
    # A click adds no provider transition: only a previously confirmed request
    # lets its re-sent message consume the existing release/adoption machinery.
    from daimon.core.session_preparation_stages import PreparedReplacement

    async def carry_history(**_options):
        # This retained case pins the bind-level transfer rule: its transfer is
        # supplied by the test, rather than derived from adapter ingress.
        # Workspace checkpoint execution is independent of provider selection.
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="history", user_prefix=""
        )

    prepared = await host.prepare(
        "thread", **({"transfer": carry_history} if slot_state == "retained" else {})
    )
    after = await host.store.get_binding(host.slot)
    assert after is not None
    assert prepared.admission.agent.id == target_agent.id
    native = slot_state != "retained"
    assert after.provider == ("openai" if native else "anthropic")
    assert (prepared.session_ref is not None) is native
    if slot_state == "released":
        assert after.id == before.id
        assert after.generation == before.generation + 2
        assert after.native_refs["session"] != before.native_refs["session"]
    async with host.sessions() as db:
        assert await pending_releases(db, tenant_id=TENANT) == []
