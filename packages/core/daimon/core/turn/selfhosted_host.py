"""Tenant-scoped host composition for the self-hosted C3 driver."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from daimon.core.mux_backend import (
    TurnBackend,
    TurnBackendRequest,
    TurnRuntime,
    register_turn_backend,
)
from daimon.core.pricing import ProviderPrice
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import reauthorize
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError
from daimon.core.turn.io import TurnCodecRequest, TurnIO, register_turn_codec
from daimon.core.turn.persistence import current_persistence
from daimon.core.turn.prepare import (
    PreparedTurn,
    ProviderPreparationRequest,
    register_turn_preparation,
)
from daimon.core.turn.selfhosted_codec import PROFILE
from daimon.core.turn.selfhosted_io import SelfHostedTurnIO
from daimon.core.turn.selfhosted_state import invocation_baseline, skipped_skill_notice
from daimon.core.usage_recording import record_provider_usage
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import Agent, ProviderBinding, Session, SessionSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.selfhosted import Authorization, SelfHostedDriver, ToolsResolver, Transport
from mux.errors import BindingConflict, ContinuityLost, ScopeViolation, UnsupportedCapability
from mux.state.lease import Lease, LeaseBusy, Slot, StaleFence
from mux.state.operations import request_digest
from mux.state.store import StateStore


@dataclass(frozen=True)
class SelfHostedHostRuntime(TurnRuntime):
    account_scope_id: str
    session_plan: Callable[[ProviderPreparationRequest], Awaitable[SessionSpec]]
    agents: Agents
    environments: Environments
    authorization: Authorization
    model: str
    price: ProviderPrice
    infrastructure_usd: Decimal
    max_tool_calls: int = 100
    deadline_s: float = 300
    tools_resolver: ToolsResolver | None = None
    record_usage: Callable[[UsageObservation, str], Awaitable[bool]] | None = None
    finalize: Callable[[], Awaitable[None]] | None = None
    history_cursor: Callable[[ResourceRef], Awaitable[int]] | None = None
    explicit_ask: Callable[[str], bool] | None = None
    skipped_notice: Callable[[], str] | None = None


def runtime_for(
    runtime: TurnRuntime | None, revision: ConfigRevision | None
) -> SelfHostedHostRuntime:
    if not isinstance(runtime, SelfHostedHostRuntime) or revision is None:
        raise UnsupportedCapability(("selfhosted_host_runtime",), PROFILE)
    if (
        revision.profile != PROFILE
        or revision.backend != "selfhosted"
        or revision.model != runtime.model
        or not runtime.model.strip()
        or not runtime.account_scope_id.strip()
    ):
        raise UnsupportedCapability(("selfhosted_host_model",), PROFILE)
    if runtime.price.provider != "selfhosted" or runtime.price.model != runtime.model:
        raise ValueError("self-hosted price differs from admitted deployment")
    if not runtime.infrastructure_usd.is_finite() or runtime.infrastructure_usd < 0:
        raise ValueError("self-hosted infrastructure cost must be explicit and nonnegative")
    return runtime


def driver(
    runtime: SelfHostedHostRuntime,
    revision: ConfigRevision,
    scope: Scope,
    binding: ProviderBinding | None = None,
    *,
    store: StateStore | None = None,
) -> SelfHostedDriver:
    def lookup(authorized: Scope, id_: str) -> ProviderBinding | None:
        if authorized != scope:
            raise ScopeViolation(id_, "self-hosted binding has a foreign scope")
        return (
            binding if binding is not None and binding.native_refs.get("session") == id_ else None
        )

    async def high_water(target: ResourceRef) -> int:
        if (
            binding is None
            or store is None
            or target.provider != "selfhosted"
            or target.kind != "session"
            or target.id != binding.native_refs.get("session")
            or target.tenant_id != scope.tenant_id
            or target.account_id != scope.account_id
        ):
            raise ScopeViolation(target.id, "persisted cursor requires the bound session")
        projection = await store.projection(target.id)
        if projection is None:
            return 0
        if projection.cursor.isdecimal():
            return int(projection.cursor)
        # A host notice can own the projection cursor; its local commit sequence
        # is separate from the harness's native ordering domain.
        after, highest = -1, 0
        while events := await store.read_events(target.id, after=after):
            for event in events:
                cursor = event.native.cursor
                if (
                    event.native.provider == "selfhosted"
                    and cursor is not None
                    and cursor.isdecimal()
                ):
                    highest = max(highest, int(cursor))
            after = events[-1].sequence
        return highest

    return SelfHostedDriver(
        cast(Transport, runtime.transport_factory(revision, scope)),
        account_scope_id=runtime.account_scope_id,
        agents=runtime.agents,
        environments=runtime.environments,
        authorization=runtime.authorization,
        binding_lookup=lookup,
        deployment=runtime.model,
        max_tool_calls=runtime.max_tool_calls,
        deadline_s=runtime.deadline_s,
        tools_resolver=runtime.tools_resolver,
        high_water=high_water if binding is not None and store is not None else None,
    )


def _backend(request: TurnBackendRequest) -> TurnBackend:
    runtime = runtime_for(request.runtime, request.config)
    active = current_persistence.get()
    if active is None or request.session is None or request.config is None:
        raise ScopeViolation(
            request.session_id, "self-hosted backend requires a persisted admitted turn"
        )
    active.check_session(request.scope, request.session)
    if active.binding.config_revision != request.config.local:
        raise ScopeViolation(
            request.session_id, "self-hosted binding differs from admitted revision"
        )
    return TurnBackend(
        driver(runtime, request.config, request.scope, active.binding, store=active.store),
        request.session,
    )


def _codec(request: TurnCodecRequest) -> TurnIO:
    runtime = runtime_for(request.runtime, request.config)
    active = request.persistence
    if active is None or current_persistence.get() is not active:
        raise ScopeViolation(
            request.session.id, "self-hosted codec requires an active persisted turn"
        )
    active.check_session(request.scope, request.session)
    return SelfHostedTurnIO(
        request.backend,
        request.scope,
        request.session,
        persistence=active,
        baseline_loader=invocation_baseline,
        explicit_ask=runtime.explicit_ask,
        history_cursor=runtime.history_cursor,
        notice_loader=lambda session: skipped_skill_notice(
            session, runtime.skipped_notice() if runtime.skipped_notice is not None else ""
        ),
    )


BUSY_RETRY_S = 5
"""How long a caller waits before the message that applies a deferred agent edit."""


class _PinnedAgents:
    """Serves the agent snapshot this turn was planned against.

    The driver reads the agent again when it builds the harness request. An
    edit that lands after preparation must not reach a turn that was already
    prepared (and billed) on the old configuration; the next turn picks it up.
    """

    def __init__(self, agents: Agents, agent: Agent) -> None:
        self._agents, self._agent = agents, agent

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
        live = await self._agents.retrieve(scope, ref)
        if ref != self._agent.ref or live.lifecycle != "active":
            # An agent archived after preparation must not execute: hand the
            # driver the live record so it refuses exactly as it would unpinned.
            # A merely edited agent keeps serving the snapshot (next turn applies it).
            return live
        return self._agent

    def __getattr__(self, name: str) -> object:
        return getattr(self._agents, name)


def _busy(now: datetime, reason: str) -> SessionBusyError:
    return SessionBusyError(
        pending_reasons=(reason,), retry_after=now + timedelta(seconds=BUSY_RETRY_S)
    )


async def _pin_planned_agent(
    runtime: SelfHostedHostRuntime, scope: Scope, spec: SessionSpec, now: datetime
) -> SelfHostedHostRuntime:
    agent = await runtime.agents.retrieve(scope, spec.agent)
    if agent.revision != spec.agent_revision:
        # The agent was edited between planning and now; the plan's tools and
        # instructions no longer match it. The next message plans afresh.
        raise _busy(now, "agent_edited")
    return replace(runtime, agents=_PinnedAgents(runtime.agents, agent))


UPDATE_LEASE_TTL = timedelta(seconds=30)


@contextlib.asynccontextmanager
async def _hold_slot(store: StateStore, slot: Slot, now: datetime) -> AsyncIterator[Lease]:
    """Hold the thread's lease while its binding is advanced.

    A turn holds this lease while it runs, so failing to take it means a turn is
    in flight. Holding it until the new generation is published keeps a rival
    turn from starting beneath the update: it cannot acquire the lease meanwhile,
    and one that acquires after sees the new generation in its binding check.
    Each update is its own holder, so concurrent updates exclude each other too.
    """
    try:
        lease = await store.acquire_lease(
            slot,
            holder="selfhosted-update:" + str(uuid4()),
            turn_id="update:" + str(uuid4()),
            now=now,
            ttl=UPDATE_LEASE_TTL,
        )
    except LeaseBusy as error:
        raise _busy(now, "session_active") from error
    try:
        yield lease
    finally:
        # A lease lost to expiry already belongs to a newer holder: leave it alone.
        with contextlib.suppress(StaleFence):
            await store.release_lease(lease)


def _config_changed(
    binding: ProviderBinding, revision: ConfigRevision, runtime: SelfHostedHostRuntime, digest: str
) -> bool:
    refs = binding.native_refs
    return (
        binding.config_revision != revision.local
        or refs.get("model") != runtime.model
        or refs.get("plan_digest") != digest
    )


async def _advance_binding(
    request: ProviderPreparationRequest,
    store: StateStore,
    *,
    backend: SelfHostedDriver,
    slot: Slot,
    binding: ProviderBinding,
    session: Session,
    spec: SessionSpec,
    runtime: SelfHostedHostRuntime,
    digest: str,
) -> ProviderBinding:
    """Move an open thread's binding to the edited agent, on the same session.

    The agent's instructions, model, tools, skills and MCP servers travel on
    every harness turn, so the session (and its workspace) is kept and only
    the pins change. A thread with a turn in flight waits instead: that turn
    finishes on the configuration it started with and the next message applies
    the edit. Written through the store's generation compare-and-swap.
    """
    now = request.now()
    async with _hold_slot(store, slot, now) as lease:
        ref = session.ref
        # Read the state again under the lease: a turn may have started since `session`.
        if (await backend.sessions.retrieve(request.scope, ref)).state != "idle":
            raise _busy(now, "session_active")
        plan = await backend.sessions.plan_update(request.scope, ref, spec)
        if plan.action == "refuse":
            raise ContinuityLost(binding.id, plan.unmet)
        key = "selfhosted:update:" + str(uuid5(NAMESPACE_URL, binding.id + digest))
        await backend.sessions.apply_update(
            request.scope, plan, expected=plan.expected_revision, key=key
        )
        advanced = (await backend.sessions.retrieve(request.scope, ref)).binding
        successor = advanced.model_copy(
            update={
                "generation": binding.generation + 1,
                "config_revision": spec.config_revision,
                "native_refs": {
                    **advanced.native_refs,
                    "model": runtime.model,
                    "plan_digest": digest,
                },
            }
        )
        try:
            # Publish only while this update still holds the slot: renewing fails
            # if the lease expired and a rival turn took it in the meantime.
            await store.renew_lease(lease, now=request.now(), ttl=UPDATE_LEASE_TTL)
            return await store.put_binding(successor, expected_generation=binding.generation)
        except (BindingConflict, StaleFence) as error:
            raise _busy(now, "binding_changed") from error


async def prepare_selfhosted(request: ProviderPreparationRequest) -> PreparedTurn:
    try:
        return await _prepare_selfhosted(request)
    except UnsupportedCapability as error:
        raise AdmissionDenied(reason="backend_unsupported") from error


async def _prepare_selfhosted(request: ProviderPreparationRequest) -> PreparedTurn:
    from daimon.core.channel_backend import BackendUnsupported, check_backend

    revision = request.admission.backend_revision
    if revision is None:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        check_backend(revision)
    except BackendUnsupported as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    runtime = runtime_for(request.deps.turn_runtimes.get(PROFILE), revision)
    if (
        not request.deps.channel_backends
        or request.deps.turn_path != "mux"
        or request.admission.shared_owner is not None
        or request.admission.hold is not None
        or request.admission.backend_revision_origin != "channel"
        or revision.thread_mode != "per_caller"
        or request.transfer is not None
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if (
        revision.channel.tenant_id != request.scope.tenant_id
        or revision.channel.platform != request.platform
        or request.scope.account_id != str(request.session_account_id)
    ):
        raise ScopeViolation(request.thread_id, "self-hosted thread differs from admitted scope")
    if not runtime.price.current_on(request.now().date()):
        raise AdmissionDenied(reason="backend_unsupported")
    current = await reauthorize(request.deps, request.admission)
    if (
        current.memory_read_only
        or current.origin_seal_ids
        or current.source_sealed
        or current.asks_before_publishing
        or current.slack_turn_context_id is not None
        or current.daimon_rules_need_driver
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    request = replace(request, admission=current)
    spec = await runtime.session_plan(request)
    if (
        spec.config_revision != revision.local
        or spec.state_mode != "continue"
        or spec.environment is None
    ):
        raise ScopeViolation(
            request.thread_id, "self-hosted plan differs from admitted configuration"
        )
    for ref, kind in ((spec.agent, "agent"), (spec.environment, "environment")):
        if (
            ref.provider != "selfhosted"
            or ref.kind != kind
            or ref.account_scope_id != runtime.account_scope_id
            or ref.tenant_id != request.scope.tenant_id
            or ref.account_id not in (None, request.scope.account_id)
        ):
            raise ScopeViolation(ref.id, "self-hosted plan contains a foreign resource")
    runtime = await _pin_planned_agent(runtime, request.scope, spec, request.now())
    store = request.deps.state_store or PostgresStateStore(request.deps.sessionmaker)
    await store.put_config_revision(revision)
    slot = Slot(
        thread=ThreadRef(channel=revision.channel, thread_id=request.thread_id),
        account_id=request.scope.account_id,
    )
    digest = request_digest({"spec": spec.model_dump(mode="json"), "model": runtime.model})
    binding = await store.get_binding(slot)
    reused = binding is not None
    if binding is not None and (
        not request.reuse_existing
        or binding.profile != PROFILE
        or binding.provider != "selfhosted"
        or binding.native_refs.get("agent") != spec.agent.id
        or binding.native_refs.get("environment") != spec.environment.id
        or binding.native_refs.get("account_scope") != runtime.account_scope_id
    ):
        raise ContinuityLost(binding.id, ("bound self-hosted configuration changed",))
    backend = driver(runtime, revision, request.scope, binding, store=store)
    if binding is None:
        # C3 has no session-create side effect: creation only verifies injected
        # resource records and derives a stable identity. The first send creates
        # harness history through its durable idempotency key.
        native = await backend.sessions.create(
            request.scope,
            spec,
            key="selfhosted:prepare:" + str(uuid5(NAMESPACE_URL, slot.model_dump_json() + digest)),
        )
        binding = ProviderBinding(
            id=str(uuid5(NAMESPACE_URL, "selfhosted:binding:" + slot.model_dump_json())),
            thread=slot.thread,
            provider="selfhosted",
            profile=PROFILE,
            native_refs={
                **native.binding.native_refs,
                "account_scope": runtime.account_scope_id,
                "model": runtime.model,
                "plan_digest": digest,
            },
            generation=1,
            config_revision=revision.local,
            legacy_account_id=request.scope.account_id,
        )
        binding = await store.put_binding(binding, expected_generation=0)
        backend = driver(runtime, revision, request.scope, binding, store=store)
        ref = native.ref
    else:
        ref = ResourceRef(
            id=binding.native_refs["session"],
            kind="session",
            provider="selfhosted",
            account_scope_id=runtime.account_scope_id,
            tenant_id=request.scope.tenant_id,
            account_id=request.scope.account_id,
        )
        native = await backend.sessions.retrieve(request.scope, ref)
        if native.state == "terminated":
            raise ContinuityLost(binding.id, ("self-hosted session terminated",))
        if _config_changed(binding, revision, runtime, digest):
            binding = await _advance_binding(
                request,
                store,
                backend=backend,
                slot=slot,
                binding=binding,
                session=native,
                spec=spec,
                runtime=runtime,
                digest=digest,
            )
            backend = driver(runtime, revision, request.scope, binding, store=store)

    async def record(*, observation: UsageObservation) -> bool:
        if observation.session != ref or observation.thread_id is not None:
            raise ScopeViolation(ref.id, "self-hosted usage differs from bound root session")
        if runtime.record_usage is not None:
            return await runtime.record_usage(observation, binding.id)
        return await record_provider_usage(
            sessionmaker=request.deps.sessionmaker,
            binding_id=binding.id,
            observation=observation,
            tenant_id=request.tenant_id,
            platform_user_id=request.external_user_id,
            provider_price=runtime.price,
            infrastructure_usd=runtime.infrastructure_usd,
            billing_grain="model_request",
            managed_session_id=ref.id,
            model_id=runtime.model,
            markup=request.deps.markup,
            channel_id=revision.channel.channel_id,
        )

    return PreparedTurn(
        admission=current,
        ma_session_id=ref.id,
        mapping_id=None,
        watermark=None,
        reused=reused,
        session_account_id=request.session_account_id,
        _record=record,
        _finalize=runtime.finalize,
        backend=backend,
        session_ref=ref,
        runtime=runtime,
    )


def register_host() -> None:
    register_turn_backend(PROFILE, _backend, fallback=True)
    register_turn_codec(PROFILE, _codec, fallback=True)
    register_turn_preparation(PROFILE, prepare_selfhosted, fallback=True)


register_host()
