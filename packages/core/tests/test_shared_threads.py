"""Opt-in shared threads (M1): C01, C14, C15, C16, cross-tenant, legacy invisibility,
SYS-047/048 first, and the concurrent first turn.

C15 and C16 are checked here on Daimon's own Anthropic port
(`daimon.core.mux_backend.managed_agents`); the mux conformance runner does
not register an Anthropic driver yet.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core._models import AgentGitHubMode
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.channel_backend import (
    channel_ref,
    current_backend_and_sharing,
    set_channel_backend,
)
from daimon.core.config import McpSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mux_backend import managed_agents
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.shared_threads import (
    SharedThread,
    may_have_shared_binding,
    record_shared_binding,
    shared_owner,
    shared_slot,
)
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import mux_state, tenant_ledger, usage_events
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.thread_sessions import create_thread_session, get_live_thread_session
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
)
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    make_fake_memory_store_handler,
    resolved_agent_env_router,
)
from daimon.testing.ma_models import ma_agent, ma_environment, ma_model_usage
from mux.contracts.config import BackendConfig, CapabilityRequirement, ConfigRevision
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.errors import (
    BindingConflict,
    ExtensionVersionError,
    MigrationUnsupported,
    UnsupportedCapability,
)
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

_NOW = datetime(2026, 10, 10, tzinfo=UTC)
_SHARED = BackendConfig(thread_mode="shared")


class _Fake:
    """MA for `create_session`: memory store, vault and session create; records requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sessions = 0
        self.created: dict[str, dict[str, object]] = {}
        self.router = MARouter()
        memory = make_fake_memory_store_handler()
        self.router.add("POST", r"/v1/memory_stores", lambda request, _m: memory(request))
        self.router.add("POST", r"/v1/vaults$", self._vault)
        self.router.add("POST", r"/v1/vaults/[^/]+/archive", self._vault)
        self.router.add(
            "GET",
            r"/v1/vaults/[^/]+/credentials",
            lambda _request, _m: httpx.Response(200, json={"data": [], "next_page": None}),
        )
        self.router.add("POST", r"/v1/sessions$", self._session)
        self.router.add(
            "POST",
            r"/v1/sessions/(?P<sid>[^/]+)/archive$",
            lambda _request, match: httpx.Response(200, json=self.created[match["sid"]]),
        )
        self.router.add(
            "GET",
            r"/v1/sessions/(?P<sid>[^/]+)$",
            lambda _request, match: httpx.Response(200, json=self.created[match["sid"]]),
        )

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.router.dispatch(request)

    def _vault(self, request: httpx.Request, _match: object) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            200,
            json={
                "id": f"vlt_{len(self.requests)}",
                "type": "vault",
                "display_name": body.get("display_name", "v"),
                "metadata": body.get("metadata"),
                "archived_at": None,
                "created_at": "2026-10-10T00:00:00Z",
            },
        )

    def _session(self, request: httpx.Request, _match: object) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.sessions += 1
        created: dict[str, object] = {
            "id": f"sess_{self.sessions}",
            "type": "session",
            "agent": {
                "id": body["agent"] if isinstance(body["agent"], str) else body["agent"]["id"],
                "mcp_servers": [],
                "model": {"id": "claude-sonnet-4-6"},
                "name": "daimon",
                "skills": [],
                "tools": [],
                "type": "agent",
                "version": 1,
            },
            "created_at": "2026-10-10T00:00:00Z",
            "outcome_evaluations": [],
            "environment_id": body["environment_id"],
            "metadata": body.get("metadata", {}),
            "resources": [
                {**resource, "id": f"res_{index}"}
                for index, resource in enumerate(body.get("resources", []))
            ],
            "stats": {},
            "status": "idle",
            "updated_at": "2026-10-10T00:00:00Z",
            "usage": {},
            "vault_ids": body.get("vault_ids", []),
        }
        self.created[str(created["id"])] = created
        return httpx.Response(200, json=created)


def _deps(sessionmaker: async_sessionmaker[AsyncSession], fake: _Fake) -> TurnDeps:
    return TurnDeps(
        anthropic=build_fake_anthropic(fake.dispatch),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1.0"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        channel_backends=True,
    )


async def _app_agent(
    session: AsyncSession, tenant: TenantRow
) -> tuple[BetaManagedAgentsAgent, BetaEnvironment]:
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_1")
    session.add(AgentGitHubMode(tenant_id=tenant.id, agent_id=agent_uuid, mode="app"))
    return agent, ma_environment(id="env_1", tenant_id=tenant.id)


def _admission(
    account_id: uuid.UUID,
    agent: BetaManagedAgentsAgent,
    env: BetaEnvironment,
    revision: ConfigRevision | None,
    *,
    ever_shared: bool = False,
) -> Admission:
    return Admission(
        account_id=account_id,
        agent=agent,
        environment=env,
        config=ResolvedConfig(agent_name="daimon", environment_name="default"),
        origin_channel_id="chan-1",
        backend_revision=revision,
        backend_ever_shared=ever_shared
        or (revision is not None and revision.thread_mode == "shared"),
    )


async def _bind(
    deps: TurnDeps,
    tenant: TenantRow,
    admission: Admission,
    *,
    user: str,
    thread_id: str = "thread-1",
) -> PreparedTurn:
    return await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id=user,
        thread_id=thread_id,
        session_account_id=admission.account_id,
        reuse_existing=True,
    )


def _slot(tenant: TenantRow, thread_id: str = "thread-1"):
    return shared_slot(tenant.id, "discord", "chan-1", thread_id)


# Pure rules


def test_the_shared_owner_is_a_pure_function_of_the_thread_and_never_a_legacy_key() -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    owner = shared_owner(shared_slot(tenant_a, "discord", "chan-1", "thread-1"))
    assert owner == shared_owner(shared_slot(tenant_a, "discord", "chan-1", "thread-1"))
    # Cross-tenant: the same platform ids in another tenant are another thread.
    assert owner != shared_owner(shared_slot(tenant_b, "discord", "chan-1", "thread-1"))
    assert owner != shared_owner(shared_slot(tenant_a, "discord", "chan-1", "thread-2"))
    legacy = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-thread-sentinel:{tenant_a}:thread-1")
    assert owner != legacy


def test_only_a_revision_that_can_share_makes_a_thread_read_its_binding() -> None:
    channel = channel_ref(uuid.uuid4(), "discord", "chan-1")
    from mux.contracts.config import resolve_default

    per_caller_once = ConfigRevision.create(channel, 1, resolve_default(None))
    per_caller_changed = ConfigRevision.create(channel, 2, resolve_default(None))
    shared = ConfigRevision.create(channel, 1, resolve_default(_SHARED))
    assert may_have_shared_binding(None, ever_shared=False) is False
    assert may_have_shared_binding(per_caller_once, ever_shared=False) is False
    # Changed but never shared: still nothing to read.
    assert may_have_shared_binding(per_caller_changed, ever_shared=False) is False
    assert may_have_shared_binding(per_caller_changed, ever_shared=True) is True
    assert may_have_shared_binding(shared, ever_shared=True) is True


# C01


async def test_c01_two_callers_share_one_workspace_with_their_own_attribution(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    agent, env = await _app_agent(db_session, tenant)
    revision = await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
    )
    await db_session.commit()
    fake = _Fake()
    deps = _deps(db_session_factory, fake)

    first = await _bind(deps, tenant, _admission(alice.id, agent, env, revision), user="alice")
    second = await _bind(deps, tenant, _admission(bob.id, agent, env, revision), user="bob")

    assert fake.sessions == 1 and first.ma_session_id == second.ma_session_id
    assert second.reused is True
    owner = shared_owner(_slot(tenant))
    assert first.session_account_id == second.session_account_id == owner
    # Attribution stays with each caller: the outcome row and the recorder.
    assert (first.admission.account_id, second.admission.account_id) == (alice.id, bob.id)
    # No caller's identity entered the shared workspace.
    sent = b"".join(request.content for request in fake.requests)
    assert str(alice.id).encode() not in sent and str(bob.id).encode() not in sent
    async with db_session_factory() as s:
        binding = await mux_state.get_binding(s, _slot(tenant))
        for account in (alice.id, bob.id):
            assert (
                await get_live_thread_session(
                    s,
                    tenant_id=tenant.id,
                    platform="discord",
                    thread_id="thread-1",
                    account_id=account,
                )
                is None
            )
    assert binding is not None
    assert binding.native_refs["session"] == first.ma_session_id
    assert (binding.legacy_account_id, binding.config_revision) == (None, revision.local)


async def test_c01_each_writer_is_charged_for_their_own_turns_in_a_shared_session(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Shared session, separate charges: every usage row names the person whose
    turn spent it, and every debit is that turn's own ledger row."""
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    agent, env = await _app_agent(db_session, tenant)
    revision = await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
    )
    await db_session.commit()
    deps = _deps(db_session_factory, _Fake())
    alices = await _bind(deps, tenant, _admission(alice.id, agent, env, revision), user="alice")
    bobs = await _bind(deps, tenant, _admission(bob.id, agent, env, revision), user="bob")
    assert alices.ma_session_id == bobs.ma_session_id

    spent = {"alice": (10, 20), "bob": (300, 400)}
    for prepared, user in ((alices, "alice"), (bobs, "bob")):
        tokens_in, tokens_out = spent[user]
        await prepared._record(  # pyright: ignore[reportPrivateUsage]
            event=BetaManagedAgentsSpanModelRequestEndEvent(
                id=f"evt_{user}",
                is_error=False,
                model_request_start_id=f"start_{user}",
                model_usage=ma_model_usage(input_tokens=tokens_in, output_tokens=tokens_out),
                processed_at=datetime.now(UTC),
                type="span.model_request_end",
            )
        )
    # A replayed event is not charged twice.
    await alices._record(  # pyright: ignore[reportPrivateUsage]
        event=BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_alice",
            is_error=False,
            model_request_start_id="start_alice",
            model_usage=ma_model_usage(input_tokens=10, output_tokens=20),
            processed_at=datetime.now(UTC),
            type="span.model_request_end",
        )
    )

    async with db_session_factory() as s:
        events = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
        ledger = await tenant_ledger.list_for_tenant(s, tenant_id=tenant.id)
        per_user = {
            user: await usage_events.list_for_tenant(s, tenant_id=tenant.id, platform_user_id=user)
            for user in spent
        }
    shared_session = alices.ma_session_id
    assert {(e.platform_user_id, e.input_tokens, e.output_tokens) for e in events} == {
        ("alice", 10, 20),
        ("bob", 300, 400),
    }
    assert {e.managed_session_id for e in events} == {shared_session}
    assert [len(per_user["alice"]), len(per_user["bob"])] == [1, 1]
    debits = sorted(row.idempotency_key for row in ledger if row.delta_usd < 0)
    assert debits == [f"turn:{shared_session}:evt_alice", f"turn:{shared_session}:evt_bob"]
    by_key = {row.idempotency_key: row.delta_usd for row in ledger}
    # Bob spent more, so his debit is the larger one: each writer's own cost.
    assert by_key[f"turn:{shared_session}:evt_bob"] < by_key[f"turn:{shared_session}:evt_alice"]


async def test_a_shared_thread_needs_an_app_mode_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    revision = await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
    )
    await db_session.commit()
    fake = _Fake()
    agent, env = (
        ma_agent(id="ag_1", tenant_id=tenant.id),
        ma_environment(id="env_1", tenant_id=tenant.id),
    )

    with pytest.raises(AdmissionDenied) as raised:
        await _bind(
            _deps(db_session_factory, fake),
            tenant,
            _admission(alice.id, agent, env, revision),
            user="alice",
        )
    assert raised.value.reason == "backend_unsupported"
    assert fake.requests == []


# Legacy history


async def test_legacy_private_history_is_never_the_shared_session(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    agent, env = await _app_agent(db_session, tenant)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        account_id=alice.id,
        ma_session_id="sess_private",
    )
    legacy_sentinel = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-thread-sentinel:{tenant.id}:thread-1")
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        account_id=legacy_sentinel,
        ma_session_id="sess_legacy_shared",
    )
    revision = await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
    )
    await db_session.commit()
    fake = _Fake()
    deps = _deps(db_session_factory, fake)

    # A thread with history is not new: it stays per caller, and bob gets his own.
    bobs = await _bind(deps, tenant, _admission(bob.id, agent, env, revision), user="bob")
    # A new thread in the same channel shares, under an owner no legacy row has.
    fresh = await _bind(
        deps, tenant, _admission(alice.id, agent, env, revision), user="alice", thread_id="thread-2"
    )

    assert bobs.session_account_id == bob.id
    assert bobs.ma_session_id not in ("sess_private", "sess_legacy_shared")
    assert fresh.session_account_id == shared_owner(_slot(tenant, "thread-2"))
    assert fresh.ma_session_id not in ("sess_private", "sess_legacy_shared")
    async with db_session_factory() as s:
        for thread_id in ("thread-1", "thread-2"):
            owner = shared_owner(_slot(tenant, thread_id))
            row = await get_live_thread_session(
                s, tenant_id=tenant.id, platform="discord", thread_id="thread-1", account_id=owner
            )
            assert row is None
        private = await get_live_thread_session(
            s, tenant_id=tenant.id, platform="discord", thread_id="thread-1", account_id=alice.id
        )
    assert private is not None and private.ma_session_id == "sess_private"


# C14


async def test_c14_a_thread_stays_on_the_revision_it_was_bound_under(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    agent, env = await _app_agent(db_session, tenant)
    channel = channel_ref(tenant.id, "discord", "chan-1")
    shared = await set_channel_backend(db_session, channel, _SHARED)
    await db_session.commit()
    fake = _Fake()
    deps = _deps(db_session_factory, fake)
    t1 = await _bind(deps, tenant, _admission(alice.id, agent, env, shared), user="alice")

    per_caller = await set_channel_backend(db_session, channel, BackendConfig())
    await db_session.commit()
    again = await _bind(
        deps, tenant, _admission(alice.id, agent, env, per_caller, ever_shared=True), user="alice"
    )
    t2 = await _bind(
        deps,
        tenant,
        _admission(alice.id, agent, env, per_caller, ever_shared=True),
        user="alice",
        thread_id="thread-2",
    )

    assert again.ma_session_id == t1.ma_session_id
    assert again.session_account_id == shared_owner(_slot(tenant))
    assert t2.session_account_id == alice.id

    # A thread pinned to a revision that can no longer run fails visibly.
    unrunnable = await set_channel_backend(
        db_session,
        channel,
        BackendConfig(
            backend="openai",
            profile="openai.persistent_workspace",
            model="gpt-5",
            thread_mode="shared",
        ),
    )
    slot3 = _slot(tenant, "thread-3")
    await record_shared_binding(
        db_session,
        SharedThread(slot=slot3, owner=shared_owner(slot3), revision=unrunnable, binding=None),
        ma_session_id="sess_openai",
    )
    await db_session.commit()
    before = len(fake.requests)
    with pytest.raises(AdmissionDenied) as raised:
        await _bind(
            deps,
            tenant,
            _admission(alice.id, agent, env, per_caller, ever_shared=True),
            user="alice",
            thread_id="thread-3",
        )
    assert raised.value.reason == "backend_unsupported"
    assert len(fake.requests) == before


# Cross-tenant


async def test_another_tenants_thread_with_the_same_ids_is_another_binding(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    fake = _Fake()
    deps = _deps(db_session_factory, fake)
    prepared: list[PreparedTurn] = []
    tenants: list[TenantRow] = []
    for _ in range(2):
        tenant = await make_tenant(db_session)
        account = await make_account(db_session, tenant=tenant)
        agent, env = await _app_agent(db_session, tenant)
        revision = await set_channel_backend(
            db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
        )
        await db_session.commit()
        prepared.append(
            await _bind(deps, tenant, _admission(account.id, agent, env, revision), user="u")
        )
        tenants.append(tenant)

    assert prepared[0].ma_session_id != prepared[1].ma_session_id
    assert prepared[0].session_account_id != prepared[1].session_account_id
    async with db_session_factory() as s:
        a = await mux_state.get_binding(s, _slot(tenants[0]))
        b = await mux_state.get_binding(s, _slot(tenants[1]))
    assert a is not None and b is not None and a.id != b.id
    assert a.thread.channel.tenant_id == str(tenants[0].id)


# Per caller stays byte-identical, with no extra reads


async def test_unconfigured_and_per_caller_binds_read_nothing_new(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    agent, env = await _app_agent(db_session, tenant)
    per_caller = await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), BackendConfig()
    )
    # Configured again, still never shared: revision 2 reads no binding either.
    reconfigured = await set_channel_backend(
        db_session,
        channel_ref(tenant.id, "discord", "chan-1"),
        BackendConfig(requires={"artifacts": CapabilityRequirement(level="required")}),
    )
    assert reconfigured.local == 2
    await db_session.commit()
    statements: list[str] = []

    def capture(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        statements.append(statement)

    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    engine = bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        traces: list[list[str]] = []
        fake = _Fake()
        # Warm the agent's memory store, provisioned on its first session only.
        await _bind(
            _deps(db_session_factory, fake),
            tenant,
            _admission(alice.id, agent, env, None),
            user="alice",
            thread_id="t-warm",
        )
        for index, revision in enumerate((None, per_caller, reconfigured)):
            statements.clear()
            prepared = await _bind(
                _deps(db_session_factory, fake),
                tenant,
                _admission(alice.id, agent, env, revision),
                user="alice",
                thread_id=f"t-{index}",
            )
            assert prepared.session_account_id == alice.id
            traces.append(list(statements))
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert traces[0] == traces[1] == traces[2]
    touched = " ".join(traces[1] + traces[2])
    for table in (
        "provider_binding",
        "journal_session",
        "channel_config_revision",
    ):
        assert table not in touched


# SYS-047 / SYS-048 still decide first


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (TenantAccessPolicy(invoker_user_ids=("staff-1",)), "invoker_not_allowed"),
        (TenantAccessPolicy(channel_rules={"chan-1": ChannelRule(writers="none")}), "writers_none"),
    ],
)
async def test_access_policy_and_protection_refuse_before_a_shared_thread_is_considered(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy,
    reason: str,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await set_channel_backend(db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED)
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    calls: list[httpx.Request] = []
    router = resolved_agent_env_router(
        ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = replace(
        _deps(db_session_factory, _Fake()),
        anthropic=build_fake_anthropic(
            lambda request: (calls.append(request), router.dispatch(request))[1]
        ),
    )

    with pytest.raises(AdmissionDenied) as raised:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )
    assert raised.value.reason == reason
    assert calls == []


# M1: the owner key is injective (Teams ids contain ':')


def test_the_owner_key_cannot_collide_on_separators() -> None:
    tenant = uuid.uuid4()
    assert shared_owner(shared_slot(tenant, "teams", "19:a", "b")) != shared_owner(
        shared_slot(tenant, "teams", "19", "a:b")
    )


# B1: two first turns in a new shared thread


@pytest.fixture
async def engine_factory(
    db_engine: AsyncEngine, db_clean: None
) -> async_sessionmaker[AsyncSession]:
    """Pooled connections of their own, so concurrent binds really race."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def test_concurrent_records_of_one_session_return_one_binding(
    engine_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with engine_factory() as session, session.begin():
        tenant = await make_tenant(session)
        revision = await set_channel_backend(
            session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
        )
    slot = _slot(tenant)
    shared = SharedThread(slot=slot, owner=shared_owner(slot), revision=revision, binding=None)

    async def record(session_id: str) -> ProviderBinding:
        async with engine_factory() as session, session.begin():
            return await record_shared_binding(session, shared, ma_session_id=session_id)

    first, second = await asyncio.gather(record("sess_x"), record("sess_x"))
    assert first == second and first.generation == 1

    # A writer that read the slot empty and lost to another session must not
    # take it over: the conflict stands.
    real_get = mux_state.get_binding
    reads = 0

    async def stale_first_read(session: AsyncSession, slot_: object) -> ProviderBinding | None:
        nonlocal reads
        reads += 1
        return None if reads == 1 else await real_get(session, slot_)  # type: ignore[arg-type]

    monkeypatch.setattr(mux_state, "get_binding", stale_first_read)
    with pytest.raises(BindingConflict):
        await record("sess_y")


async def test_c13_two_first_turns_in_a_new_shared_thread_share_one_session(
    engine_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with engine_factory() as session, session.begin():
        tenant = await make_tenant(session)
        alice = await make_account(session, tenant=tenant)
        bob = await make_account(session, tenant=tenant)
        agent, env = await _app_agent(session, tenant)
        revision = await set_channel_backend(
            session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
        )
    fake = _Fake()
    deps = _deps(engine_factory, fake)

    first, second = await asyncio.gather(
        _bind(deps, tenant, _admission(alice.id, agent, env, revision), user="alice"),
        _bind(deps, tenant, _admission(bob.id, agent, env, revision), user="bob"),
    )

    assert first.ma_session_id == second.ma_session_id
    assert fake.sessions == 1
    async with engine_factory() as session:
        binding = await mux_state.get_binding(session, _slot(tenant))
    assert binding is not None and binding.generation == 1
    assert binding.native_refs["session"] == first.ma_session_id


# C15 / C16 on Daimon's Anthropic port


async def test_c15_migrate_is_unsupported_and_leaves_the_binding_unchanged(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
    )
    slot = _slot(tenant)
    bound = await record_shared_binding(
        db_session,
        SharedThread(slot=slot, owner=shared_owner(slot), revision=revision, binding=None),
        ma_session_id="sess_1",
    )
    backend = managed_agents(build_fake_anthropic(_Fake().dispatch))
    scope = Scope(tenant_id=str(tenant.id), account_id="a", principal_id="p", authorization_id="z")
    ref = ResourceRef(
        id="sess_1",
        kind="session",
        provider="anthropic",
        account_scope_id=backend.account_scope_id,
        tenant_id=str(tenant.id),
    )
    target = await set_channel_backend(
        db_session,
        channel_ref(tenant.id, "discord", "chan-1"),
        BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-5"),
    )

    with pytest.raises(MigrationUnsupported):
        await backend.sessions.migrate(scope, ref, target, expected=bound.generation, key="k")

    assert await mux_state.get_binding(db_session, slot) == bound


def test_c16_the_port_exposes_no_raw_client_and_refuses_undeclared_extensions() -> None:
    client = build_fake_anthropic(_Fake().dispatch)
    backend = managed_agents(client)
    raw_names = {"client", "raw_client", "sdk", "raw", "anthropic"}
    for port in (backend, backend.sessions, backend.events):
        public = {name for name in dir(port) if not name.startswith("_")}
        assert not public & raw_names
        for name in public:
            # Static lookup: a property such as `models` may itself refuse.
            value = inspect.getattr_static(port, name)
            assert not isinstance(value, (AsyncAnthropic, httpx.AsyncClient))
    with pytest.raises(UnsupportedCapability):
        backend.extension(object, namespace="anthropic.undeclared", version=1)
    offered = backend.capabilities().extensions[0]
    with pytest.raises(ExtensionVersionError):
        backend.extension(object, namespace=offered.namespace, version=offered.version + 99)


# Review round 2: new threads only, no personal servers, sharing history in one read


async def test_an_existing_thread_stays_per_caller_when_the_channel_starts_sharing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    agent, env = await _app_agent(db_session, tenant)
    channel = channel_ref(tenant.id, "discord", "chan-1")
    per_caller = await set_channel_backend(db_session, channel, BackendConfig())
    await db_session.commit()
    fake = _Fake()
    deps = _deps(db_session_factory, fake)
    private = await _bind(deps, tenant, _admission(alice.id, agent, env, per_caller), user="alice")

    shared = await set_channel_backend(db_session, channel, _SHARED)
    await db_session.commit()
    again = await _bind(deps, tenant, _admission(alice.id, agent, env, shared), user="alice")
    bobs = await _bind(deps, tenant, _admission(bob.id, agent, env, shared), user="bob")
    new_thread = await _bind(
        deps, tenant, _admission(alice.id, agent, env, shared), user="alice", thread_id="thread-2"
    )

    assert again.ma_session_id == private.ma_session_id
    assert again.session_account_id == alice.id
    assert bobs.session_account_id == bob.id and bobs.ma_session_id != private.ma_session_id
    assert new_thread.session_account_id == shared_owner(_slot(tenant, "thread-2"))
    async with db_session_factory() as s:
        assert await mux_state.get_binding(s, _slot(tenant)) is None


async def test_the_sharing_history_comes_with_the_revision(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    channel = channel_ref(tenant.id, "discord", "chan-1")
    assert await current_backend_and_sharing(db_session, channel) == (None, False)
    first = await set_channel_backend(db_session, channel, BackendConfig())
    assert await current_backend_and_sharing(db_session, channel) == (first, False)
    await set_channel_backend(db_session, channel, _SHARED)
    back = await set_channel_backend(db_session, channel, BackendConfig())
    assert await current_backend_and_sharing(db_session, channel) == (back, True)


async def test_a_shared_session_carries_no_personally_connected_server(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_personal")
    db_session.add(AgentGitHubMode(tenant_id=tenant.id, agent_id=agent_uuid, mode="app"))
    await _record_personal_sign_in(
        db_session, tenant_id=tenant.id, account_id=alice.id, agent_uuid=agent_uuid
    )
    toolset: dict[str, object] = {
        "type": "mcp_toolset",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }
    agent = ma_agent(
        id="ag_personal",
        tenant_id=tenant.id,
        mcp_servers=[
            {"name": "docs", "type": "url", "url": "https://mcp.example.com/docs"},
            {"name": "team", "type": "url", "url": "https://mcp.example.com/team"},
        ],
        tools=[{**toolset, "mcp_server_name": "docs"}, {**toolset, "mcp_server_name": "team"}],
    )
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    revision = await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), _SHARED
    )
    await db_session.commit()
    fake = _Fake()

    # Alice herself connected `docs`; her turn in the shared thread still leaves it off.
    await _bind(
        _deps(db_session_factory, fake),
        tenant,
        _admission(alice.id, agent, env, revision),
        user="alice",
    )

    (create,) = [r for r in fake.requests if r.method == "POST" and r.url.path == "/v1/sessions"]
    sent = json.loads(create.content)["agent"]
    assert sent["type"] == "agent_with_overrides"
    assert [server["name"] for server in sent["mcp_servers"]] == ["team"]
    assert [tool["mcp_server_name"] for tool in sent["tools"]] == ["team"]
    assert b"mcp.example.com/docs" not in create.content


async def _record_personal_sign_in(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_uuid: uuid.UUID
) -> None:
    """One member's finished OAuth sign-in for the agent's `docs` server."""
    now = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        account_id=account_id,
        target="docs",
        mcp_server_url="https://mcp.example.com/docs",
        requester_platform_user_id="requester-connected",
        channel_id="chan-1",
        expires_at=now + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_personal",
        target_name="daimon",
        requested_work=None,
    )
    flow = await flows_store.create_flow(
        session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_uuid,
        server_name="docs",
        mcp_server_url="https://mcp.example.com/docs",
        redirect_uri="https://d.example/oauth/mcp/callback",
        code_verifier="verifier",
        expires_at=now + timedelta(minutes=10),
    )
    await flows_store.consume_flow(session, state=flow.state, now=now)
    await flows_store.mark_flow_completed(session, state=flow.state, now=now)
