"""M1 across backends: opt-in shared threads with explicit OpenAI or Gemini channels.

Offline. In this release neither alternate host shares a thread: G1 (#689) and
G2 admit OpenAI and Gemini per caller only. So the real evidence here is a
typed refusal: a shared OpenAI or Gemini channel is refused as
`backend_unsupported` at admission and again before dispatch, so the real
host preparer never sees a shared owner, with no native I/O, binding or
session row (`test_g1_refuses_a_shared_thread_visibly`,
`test_g2_refuses_a_shared_thread_visibly`,
`test_a_backend_that_does_not_share_never_sees_a_shared_owner`).

The seam a future sharing host would use is proven with stand-in preparers
under an isolated stand-in admission policy (the `providers` fixture admits
only the stand-in revisions): each caller reaches the preparer as the
thread's shared owner on one native session, the binding records the
provider and pinned revision, a channel switch leaves the thread on its
first provider (C14), the identity survives a restart, the journal takes
only the shared slot's lease, and an unconfigured channel reaches none of it.
These are seam contracts, not certification: foreign shared execution,
credentials, caller billing and C01/C14 on a real foreign host stay PENDING.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.core import shared_threads
from daimon.core.channel_backend import (
    BackendUnsupported,
    channel_ref,
    check_backend,
    set_channel_backend,
)
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.shared_threads import shared_owner, shared_slot
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn import openai_host
from daimon.core.turn import prepare as preparation
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest, bind_session
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import BackendConfig, ConfigRevision
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding, SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.errors import ScopeViolation
from mux.state.journal import JournalEntry
from mux.state.lease import Slot
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

OPENAI = "openai.persistent_workspace"
GEMINI = "gemini.inline_reuse"
# Models no real host admits for sharing: these revisions exist only for the stand-ins.
STAND_INS = frozenset({(OPENAI, "gpt-stand-in"), (GEMINI, "gemini-stand-in")})
CONFIGS = {
    OPENAI: BackendConfig(
        backend="openai", profile=OPENAI, model="gpt-stand-in", thread_mode="shared"
    ),
    GEMINI: BackendConfig(
        backend="gemini", profile=GEMINI, model="gemini-stand-in", thread_mode="shared"
    ),
}


@dataclass
class _Provider:
    """A stand-in host preparer: one native session per (thread, session owner)."""

    profile: str
    calls: list[ProviderPreparationRequest] = field(
        default_factory=list[ProviderPreparationRequest]
    )
    sessions: dict[tuple[str, uuid.UUID], str] = field(
        default_factory=dict[tuple[str, uuid.UUID], str]
    )

    async def __call__(self, request: ProviderPreparationRequest) -> PreparedTurn:
        self.calls.append(request)
        key = (request.thread_id, request.session_account_id)
        reused = key in self.sessions
        native = self.sessions.setdefault(key, f"{self.profile}:{len(self.sessions)}")
        provider = self.profile.split(".")[0]
        ref = ResourceRef(
            id=native,
            kind="session",
            provider=provider,  # pyright: ignore[reportArgumentType]
            account_scope_id=f"{provider}-project",
            tenant_id=request.scope.tenant_id,
            account_id=request.scope.account_id,
        )

        if request.admission.shared_owner is None and not reused:
            revision = request.admission.backend_revision
            assert revision is not None
            async with request.deps.sessionmaker() as session:
                await mux_state.put_binding(
                    session,
                    ProviderBinding(
                        id=str(uuid.uuid4()),
                        thread=ThreadRef(channel=revision.channel, thread_id=request.thread_id),
                        provider=revision.backend,
                        profile=revision.profile,
                        native_refs={"session": native},
                        generation=1,
                        config_revision=revision.local,
                        legacy_account_id=request.scope.account_id,
                    ),
                    expected_generation=0,
                )
                await session.commit()

        async def record(event: object) -> None:
            raise AssertionError("preparation never bills")

        return PreparedTurn(
            admission=request.admission,
            ma_session_id=native,
            mapping_id=None,
            watermark=None,
            reused=reused,
            session_account_id=request.session_account_id,
            _record=record,
            session_ref=ref,
        )


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Provider]:
    """Both profiles registered and runnable, as G1/G2's registration lines make them."""
    # The real OpenAI host registers lazily; register it first so the stand-in
    # replaces it for this test instead of colliding with a later registration.
    openai_host.register_host()
    registered = {profile: _Provider(profile) for profile in CONFIGS}
    for profile, provider in registered.items():
        monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, provider)  # pyright: ignore[reportPrivateUsage]
    # The stand-ins' own admission policy, isolated from the real hosts' release
    # policy (which admits OpenAI and Gemini per caller only): exactly the
    # stand-in revisions are admitted, everything else goes to the real check.
    real_check = shared_threads.check_backend

    def stand_in_policy(revision: ConfigRevision) -> object:
        if (revision.profile, revision.model) in STAND_INS:
            return None
        return real_check(revision)

    monkeypatch.setattr(shared_threads, "check_backend", stand_in_policy)
    return registered


def _deps(sessionmaker: async_sessionmaker[AsyncSession]) -> TurnDeps:
    def no_anthropic(request: Any) -> Any:
        raise AssertionError(f"an explicit provider turn reached Anthropic: {request.url.path}")

    return TurnDeps(
        anthropic=build_fake_anthropic(no_anthropic),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/unused"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="mux",
        channel_backends=True,
        tool_safety=OPEN_TOOL_SAFETY,
    )


@dataclass
class _World:
    tenant: Any
    alice: uuid.UUID
    bob: uuid.UUID
    agent: Any
    env: Any


async def _world(session: AsyncSession) -> _World:
    tenant = await make_tenant(session)
    alice = await make_account(session, tenant=tenant)
    bob = await make_account(session, tenant=tenant)
    await session.commit()
    return _World(
        tenant, alice.id, bob.id, ma_agent(id="ag_host", tenant_id=tenant.id), ma_environment()
    )


def _admission(world: _World, account: uuid.UUID, revision: ConfigRevision | None) -> Admission:
    return Admission(
        account_id=account,
        agent=world.agent,
        environment=world.env,
        config=ResolvedConfig(agent_name="host", environment_name="host"),
        origin_channel_id="chan-x",
        backend_revision=revision,
        backend_revision_origin="channel" if revision is not None else None,
        backend_ever_shared=revision is not None and revision.thread_mode == "shared",
    )


async def _bind(
    deps: TurnDeps,
    world: _World,
    account: uuid.UUID,
    revision: ConfigRevision | None,
    *,
    user: str,
    thread_id: str = "thread-1",
    ever_shared: bool = False,
) -> PreparedTurn:
    admission = _admission(world, account, revision)
    if ever_shared:
        admission = replace(admission, backend_ever_shared=True)
    return await bind_session(
        deps,
        admission,
        tenant_id=world.tenant.id,
        platform="slack",
        external_user_id=user,
        thread_id=thread_id,
        session_account_id=account,
        reuse_existing=True,
    )


def _slot(world: _World, thread_id: str = "thread-1") -> Slot:
    return shared_slot(world.tenant.id, "slack", "chan-x", thread_id)


@pytest.mark.parametrize("profile", [GEMINI])
async def test_a_shared_thread_on_an_explicit_backend_keeps_one_identity(
    profile: str,
    providers: dict[str, _Provider],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(world.tenant.id, "slack", "chan-x"), CONFIGS[profile]
    )
    await db_session.commit()
    deps = _deps(db_session_factory)

    first = await _bind(deps, world, world.alice, revision, user="alice")
    second = await _bind(deps, world, world.bob, revision, user="bob")

    owner = shared_owner(_slot(world))
    provider = providers[profile]
    # Admission: the explicit revision reaches the provider, unchanged.
    assert [c.admission.backend_revision for c in provider.calls] == [revision, revision]
    # Binding: both callers reach the provider as the thread's owner, one native session.
    assert [c.session_account_id for c in provider.calls] == [owner, owner]
    assert first.ma_session_id == second.ma_session_id and second.reused
    assert (first.admission.account_id, second.admission.account_id) == (world.alice, world.bob)
    async with db_session_factory() as s:
        binding = await mux_state.get_binding(s, _slot(world))
    assert binding is not None
    assert (binding.provider, binding.profile) == (CONFIGS[profile].backend, profile)
    assert binding.native_refs["session"] == first.ma_session_id
    assert (binding.generation, binding.config_revision) == (1, revision.local)
    assert binding.id == str(owner) and binding.legacy_account_id is None
    assert providers[OPENAI].calls == []


async def test_c14_a_provider_switch_moves_new_threads_only(
    providers: dict[str, _Provider],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A: Gemini, shared. B: OpenAI, per caller (OpenAI does not share yet)."""
    world = await _world(db_session)
    channel = channel_ref(world.tenant.id, "slack", "chan-x")
    on_gemini = await set_channel_backend(db_session, channel, CONFIGS[GEMINI])
    await db_session.commit()
    deps = _deps(db_session_factory)
    t1 = await _bind(deps, world, world.alice, on_gemini, user="alice")

    on_openai = await set_channel_backend(
        db_session, channel, BackendConfig(backend="openai", profile=OPENAI, model="gpt-6-luna")
    )
    await db_session.commit()
    t1_again = await _bind(deps, world, world.bob, on_openai, user="bob", ever_shared=True)
    t2 = await _bind(
        deps, world, world.alice, on_openai, user="alice", thread_id="thread-2", ever_shared=True
    )

    # T1 stays on A (Gemini, shared, its pinned revision); T2 runs on B (OpenAI, per caller).
    assert t1_again.ma_session_id == t1.ma_session_id
    assert t1_again.admission.backend_revision == on_gemini
    assert t1_again.session_account_id == shared_owner(_slot(world))
    assert [c.thread_id for c in providers[GEMINI].calls] == ["thread-1", "thread-1"]
    (openai_call,) = providers[OPENAI].calls
    assert (openai_call.thread_id, openai_call.session_account_id) == ("thread-2", world.alice)
    assert t2.admission.backend_revision == on_openai and t2.admission.shared_owner is None
    async with db_session_factory() as s:
        b1 = await mux_state.get_binding(s, _slot(world))
        b2 = await mux_state.get_binding(s, _slot(world, "thread-2"))
    assert b1 is not None and (b1.provider, b1.config_revision) == ("gemini", on_gemini.local)
    assert b2 is None  # a per-caller thread has no shared binding


async def test_the_identity_survives_a_restart_and_owns_the_journal(
    providers: dict[str, _Provider],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(world.tenant.id, "slack", "chan-x"), CONFIGS[GEMINI]
    )
    await db_session.commit()
    first = await _bind(_deps(db_session_factory), world, world.alice, revision, user="alice")

    # A new process: fresh deps, the same database. The thread resolves the same.
    again = await _bind(_deps(db_session_factory), world, world.bob, revision, user="bob")
    assert again.ma_session_id == first.ma_session_id
    assert again.session_account_id == first.session_account_id == shared_owner(_slot(world))

    store = PostgresStateStore(db_session_factory, trust_caller_clock=True)
    now = datetime.now(UTC)
    shared_lease = await store.acquire_lease(
        _slot(world), holder="w", turn_id="t", now=now, ttl=timedelta(minutes=1)
    )
    event = Event(
        id="ev_1",
        session_id=first.ma_session_id,
        sequence=0,
        type="agent.message",
        observed_at=now,
        authority="record",
        payload={"item_id": "item_1", "content": []},
        native=NativeProvenance(provider="gemini", api_revision="offline"),
    )
    session_ref = ResourceRef(
        id=first.ma_session_id, kind="session", provider="gemini", account_scope_id="gemini-project"
    )
    appended = await store.append_events(
        session_ref,
        [JournalEntry(source_key="item_1", event=event)],
        fence=shared_lease,
        cursor="c1",
        now=now,
    )
    assert [e.sequence for e in appended.appended] == [0]
    # A caller's private lease cannot write the shared thread's journal.
    private = Slot(thread=_slot(world).thread, account_id=str(world.alice))
    private_lease = await store.acquire_lease(
        private, holder="w", turn_id="t", now=now, ttl=timedelta(minutes=1)
    )
    with pytest.raises(ScopeViolation):
        await store.append_events(
            session_ref,
            [JournalEntry(source_key="item_2", event=event.model_copy(update={"id": "ev_2"}))],
            fence=private_lease,
            cursor="c2",
            now=now,
        )


async def test_an_unconfigured_channel_never_reaches_a_provider_or_the_shared_slot(
    providers: dict[str, _Provider],
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session)
    seen: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def legacy_bind(_deps: TurnDeps, admission: Admission, **kwargs: Any) -> object:
        seen.append((kwargs["session_account_id"], admission.shared_owner))
        return object()

    async def no_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an unconfigured turn must not read a shared binding")

    monkeypatch.setattr(preparation, "bind_session_impl", legacy_bind)
    monkeypatch.setattr(preparation, "resolve_shared_thread", no_resolution)
    deps = _deps(db_session_factory)
    for account, user in ((world.alice, "alice"), (world.bob, "bob")):
        # The stand-in result has no session id; the decision is already recorded.
        with contextlib.suppress(AttributeError):
            await _bind(deps, world, account, None, user=user)

    assert seen == [(world.alice, None), (world.bob, None)]
    assert all(not p.calls for p in providers.values())
    async with db_session_factory() as s:
        assert await mux_state.get_binding(s, _slot(world)) is None


# The real release policy: neither alternate host shares, so a shared revision is
# refused before dispatch and the registered host preparer never sees a shared owner.

RELEASED = {
    OPENAI: BackendConfig(
        backend="openai", profile=OPENAI, model="gpt-6-luna", thread_mode="shared"
    ),
    GEMINI: BackendConfig(
        backend="gemini", profile=GEMINI, model="gemini-3.8-flash", thread_mode="shared"
    ),
}


@pytest.mark.parametrize("profile", [OPENAI, GEMINI])
async def test_a_backend_that_does_not_share_never_sees_a_shared_owner(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.turn.runtimes import ensure_channel_runtime

    assert ensure_channel_runtime(profile)
    assert profile in preparation._PROVIDER_PREPARATIONS  # pyright: ignore[reportPrivateUsage]
    seen: list[ProviderPreparationRequest] = []

    async def spy(request: ProviderPreparationRequest) -> PreparedTurn:
        seen.append(request)
        raise AssertionError("a refused shared thread reached the host preparer")

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, spy)  # pyright: ignore[reportPrivateUsage]
    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(world.tenant.id, "slack", "chan-x"), RELEASED[profile]
    )
    await db_session.commit()
    with pytest.raises(AdmissionDenied) as raised:
        await _bind(_deps(db_session_factory), world, world.alice, revision, user="alice")
    assert raised.value.reason == "backend_unsupported"
    assert seen == []
    async with db_session_factory() as s:
        assert await mux_state.get_binding(s, _slot(world)) is None


# G1 (#689) for real: the OpenAI host refuses a shared thread before any native I/O.


async def test_g1_refuses_a_shared_thread_visibly(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    openai_host.register_host()
    requests: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        raise AssertionError(f"native OpenAI I/O for a refused shared thread: {request.url.path}")

    async def plan(_request: object) -> SessionSpec:
        raise AssertionError("a refused shared thread must not plan a session")

    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session,
        channel_ref(world.tenant.id, "slack", "chan-x"),
        BackendConfig(backend="openai", profile=OPENAI, model="gpt-6-luna", thread_mode="shared"),
    )
    await db_session.commit()
    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    ) as sdk:
        runtime = OpenAIHostRuntime(
            lambda _revision, _scope: SDKTransport(sdk),
            OpenAIRecoveryJournal(),
            OpenAIUsageRevisions(),
            account_scope_id="project",
            session_plan=plan,
            authorization=lambda _scope, _kind, _id: True,
            controls=SessionControls(
                model="gpt-6-luna",
                multi_agent_enabled=False,
                container_size="small",
                spend_limit_usd_cents=4,
            ),
        )
        deps = replace(_deps(db_session_factory), turn_runtimes={OPENAI: runtime})
        for account, user in ((world.alice, "alice"), (world.bob, "bob")):
            with pytest.raises(AdmissionDenied) as raised:
                await _bind(deps, world, account, revision, user=user)
            assert raised.value.reason == "backend_unsupported"

    assert requests == []
    # A production admission refuses it even earlier, at channel resolution.
    with pytest.raises(BackendUnsupported):
        check_backend(revision)
    async with db_session_factory() as s:
        assert await mux_state.get_binding(s, _slot(world)) is None
        for account in (world.alice, world.bob, shared_owner(_slot(world))):
            assert (
                await get_live_thread_session(
                    s,
                    tenant_id=world.tenant.id,
                    platform="slack",
                    thread_id="thread-1",
                    account_id=account,
                )
                is None
            )


# G2 for real: the registered Gemini host is refused a shared thread before it runs.


async def test_g2_refuses_a_shared_thread_visibly(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.turn.runtimes import ensure_channel_runtime

    assert ensure_channel_runtime(GEMINI)
    real = preparation._PROVIDER_PREPARATIONS[GEMINI]  # pyright: ignore[reportPrivateUsage]
    reached: list[ProviderPreparationRequest] = []

    async def counted(request: ProviderPreparationRequest) -> PreparedTurn:
        reached.append(request)
        return await real(request)

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, GEMINI, counted)  # pyright: ignore[reportPrivateUsage]
    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(world.tenant.id, "slack", "chan-x"), RELEASED[GEMINI]
    )
    await db_session.commit()
    # No Gemini runtime is wired: any native I/O would need one, so none can happen.
    deps = _deps(db_session_factory)
    for account, user in ((world.alice, "alice"), (world.bob, "bob")):
        with pytest.raises(AdmissionDenied) as raised:
            await _bind(deps, world, account, revision, user=user)
        assert raised.value.reason == "backend_unsupported"

    assert reached == []
    # A production admission refuses it even earlier, at channel resolution, while
    # the released per-caller Gemini configuration stays admitted.
    with pytest.raises(BackendUnsupported):
        check_backend(revision)
    per_caller = await set_channel_backend(
        db_session,
        channel_ref(world.tenant.id, "slack", "chan-y"),
        RELEASED[GEMINI].model_copy(update={"thread_mode": "per_caller"}),
    )
    check_backend(per_caller)
    async with db_session_factory() as s:
        assert await mux_state.get_binding(s, _slot(world)) is None
        for account in (world.alice, world.bob, shared_owner(_slot(world))):
            assert (
                await get_live_thread_session(
                    s,
                    tenant_id=world.tenant.id,
                    platform="slack",
                    thread_id="thread-1",
                    account_id=account,
                )
                is None
            )
