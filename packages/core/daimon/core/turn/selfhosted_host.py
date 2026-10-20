"""Tenant-scoped host composition for the self-hosted C3 driver."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from daimon.core.mux_backend import (
    TurnBackend,
    TurnBackendRequest,
    TurnRuntime,
    register_turn_backend,
)
from daimon.core.pricing import ProviderPrice
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import reauthorize
from daimon.core.turn.errors import AdmissionDenied
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
from mux.contracts.resources import ProviderBinding, SessionSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.selfhosted import SelfHostedDriver
from mux.drivers.selfhosted.context import Authorization
from mux.drivers.selfhosted.events import ToolsResolver
from mux.drivers.selfhosted.transport import Transport
from mux.errors import ContinuityLost, ScopeViolation, UnsupportedCapability
from mux.state.lease import Slot
from mux.state.operations import request_digest


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
) -> SelfHostedDriver:
    def lookup(authorized: Scope, id_: str) -> ProviderBinding | None:
        if authorized != scope:
            raise ScopeViolation(id_, "self-hosted binding has a foreign scope")
        return (
            binding if binding is not None and binding.native_refs.get("session") == id_ else None
        )

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
        driver(runtime, request.config, request.scope, active.binding), request.session
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
        or request.worker is not None
        or request.native_budget_usd is not None
        or request.admission.shared_owner is not None
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
        or binding.config_revision != revision.local
        or binding.native_refs.get("agent") != spec.agent.id
        or binding.native_refs.get("environment") != spec.environment.id
        or binding.native_refs.get("account_scope") != runtime.account_scope_id
        or binding.native_refs.get("model") != runtime.model
        or binding.native_refs.get("plan_digest") != digest
    ):
        raise ContinuityLost(binding.id, ("bound self-hosted configuration changed",))
    backend = driver(runtime, revision, request.scope, binding)
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
        backend = driver(runtime, revision, request.scope, binding)
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
