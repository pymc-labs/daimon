"""Explicit OpenAI host composition; no SDK/key discovery or default selection.

Operators inject an authorized native session plan and private driver transport.
The host publishes its actual binding before running, then A4 owns the lease,
send claims and journal. Unknown container cost remains durable pending usage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
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
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.io import TurnCodecRequest, TurnIO, register_turn_codec
from daimon.core.turn.openai_codec import PROFILE
from daimon.core.turn.openai_io import OpenAITurnIO
from daimon.core.turn.openai_state import (
    OpenAIRecoveryJournal,
    OpenAIUsageRevisions,
    invocation_baseline,
)
from daimon.core.turn.persistence import UncertainSend, current_persistence
from daimon.core.turn.prepare import (
    PreparedTurn,
    ProviderPreparationRequest,
    register_turn_preparation,
)
from daimon.core.usage_recording import record_provider_usage
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding, Session, SessionSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import Authorization
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import Transport
from mux.errors import ContinuityLost, ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.lease import Slot
from mux.state.operations import request_digest
from mux.state.store import StateStore


async def unknown_infrastructure(observation: UsageObservation) -> Decimal | None:
    return None


@dataclass(frozen=True)
class OpenAIHostRuntime(TurnRuntime):
    """Explicit native plan, project, authorization and accounting configuration.

    Inherited journal/usage_store must be the durable host adapters below;
    memory test implementations are not accepted by the runnable factory.
    The plan resolves real provisioned OpenAI agent/environment/skill refs. It
    cannot use Anthropic admission ids as native resources. No native agent
    atomic revision pin is claimed. Changes to bound threads refuse pending a
    provider-native continuity policy, rather than silently erase a workspace.
    """

    account_scope_id: str
    session_plan: Callable[[ProviderPreparationRequest], Awaitable[SessionSpec]]
    authorization: Authorization
    controls: SessionControls
    price: ProviderPrice | None = None
    infrastructure: Callable[[UsageObservation], Awaitable[Decimal | None]] = unknown_infrastructure


def runtime_for(runtime: TurnRuntime | None, revision: ConfigRevision | None) -> OpenAIHostRuntime:
    # Validate before transport_factory can access credentials or make requests.
    if not isinstance(runtime, OpenAIHostRuntime) or revision is None:
        raise UnsupportedCapability(("openai_host_runtime",), PROFILE)
    if (
        revision.profile != PROFILE
        or revision.backend != "openai"
        or revision.model != "gpt-6-luna"
    ):
        raise UnsupportedCapability(("openai_host_model",), PROFILE)
    if (
        not runtime.account_scope_id.strip()
        or runtime.controls.model != "gpt-6-luna"
        or runtime.controls.multi_agent_enabled is not False
        or runtime.controls.container_size != "small"
        or runtime.controls.spend_limit_usd_cents is None
        or not isinstance(runtime.journal, OpenAIRecoveryJournal)
        or not isinstance(runtime.usage_store, OpenAIUsageRevisions)
    ):
        raise UnsupportedCapability(("openai_host_controls_and_durable_state",), PROFILE)
    if runtime.price is not None and (
        runtime.price.provider != "openai" or runtime.price.model != "gpt-6-luna"
    ):
        raise ValueError("OpenAI host price differs from admitted model")
    return runtime


def driver(
    runtime: OpenAIHostRuntime,
    revision: ConfigRevision,
    scope: Scope,
    binding: ProviderBinding | None = None,
) -> OpenAIDriver:
    def lookup(request_scope: Scope, id_: str) -> ProviderBinding | None:
        if request_scope != scope:
            raise ScopeViolation(id_, "OpenAI native binding has a foreign scope")
        return (
            binding if binding is not None and binding.native_refs.get("session") == id_ else None
        )

    return OpenAIDriver(
        cast(Transport, runtime.transport_factory(revision, scope)),
        account_scope_id=runtime.account_scope_id,
        journal=cast(OpenAIRecoveryJournal, runtime.journal),
        usage_revisions=cast(OpenAIUsageRevisions, runtime.usage_store),
        authorization=runtime.authorization,
        binding_lookup=lookup,
        session_controls=runtime.controls,
    )


def _backend(request: TurnBackendRequest) -> TurnBackend:
    runtime = runtime_for(request.runtime, request.config)
    active = current_persistence.get()
    if active is None or request.session is None or request.config is None:
        raise ScopeViolation(
            request.session_id, "OpenAI backend requires a persisted admitted turn"
        )
    active.check_session(request.scope, request.session)
    if active.binding.config_revision != request.config.local:
        raise ScopeViolation(request.session_id, "OpenAI binding differs from admitted revision")
    return TurnBackend(
        driver(runtime, request.config, request.scope, active.binding), request.session
    )


def _codec(request: TurnCodecRequest) -> TurnIO:
    runtime_for(request.runtime, request.config)
    active = request.persistence
    if active is None or current_persistence.get() is not active:
        raise ScopeViolation(request.session.id, "OpenAI codec requires an active persisted turn")
    active.check_session(request.scope, request.session)
    return OpenAITurnIO(
        request.backend,
        request.scope,
        request.session,
        persistence=active,
        baseline_loader=invocation_baseline,
    )


def _check_spec(
    spec: SessionSpec, runtime: OpenAIHostRuntime, scope: Scope, revision: ConfigRevision
) -> None:
    refs = (spec.agent,) + ((spec.environment,) if spec.environment is not None else ())
    if spec.config_revision != revision.local or spec.state_mode != "continue":
        raise ScopeViolation("preparation", "OpenAI plan differs from admitted revision")
    for ref in refs:
        if (
            ref.provider != "openai"
            or ref.account_scope_id != runtime.account_scope_id
            or ref.tenant_id != scope.tenant_id
            or ref.account_id != scope.account_id
        ):
            raise ScopeViolation(ref.id, "OpenAI plan contains a foreign native resource")
    if spec.agent.kind != "agent" or (
        spec.environment is not None and spec.environment.kind != "environment"
    ):
        raise ScopeViolation("preparation", "OpenAI plan has the wrong resource kind")
    if spec.agent_revision.local != 0 or spec.agent_revision.native is not None:
        raise UnsupportedCapability(("agent_revision_pin",), PROFILE)


async def _create(
    request: ProviderPreparationRequest,
    store: StateStore,
    slot: Slot,
    backend: OpenAIDriver,
    spec: SessionSpec,
    runtime: OpenAIHostRuntime,
) -> Session:
    # Stable per empty slot, including after a crash. Changed config/plan gets a
    # digest conflict, not a fresh operation which might create a second session.
    key = "openai:prepare:" + str(uuid5(NAMESPACE_URL, slot.model_dump_json()))
    digest = request_digest(
        {"spec": spec.model_dump(mode="json"), "controls": runtime.controls.model_dump(mode="json")}
    )
    begun = await store.begin_operation(
        request.scope,
        key=key,
        request_digest=digest,
        operation_id=key,
        slot=slot,
        now=request.now(),
    )
    record = begun.record
    if record.operation.status == "processed":
        return Session.model_validate(dict(record.result))
    if record.operation.status != "pending":
        raise UncertainSend("OpenAI session creation was claimed; reconcile before provisioning")
    lease = await store.acquire_lease(
        slot, holder=str(uuid4()), turn_id=key, now=request.now(), ttl=timedelta(minutes=5)
    )
    try:
        # The lease winner rechecks after acquisition before claiming or creating.
        if await store.get_binding(slot) is not None:
            raise ScopeViolation("preparation", "OpenAI slot was bound during preparation")
        final = await reauthorize(request.deps, request.admission)
        if final != request.admission:
            raise AdmissionDenied(reason="backend_unsupported")
        await store.claim_send(request.scope, key, now=request.now(), fence=lease)
        try:
            async with asyncio.timeout(
                min(120, (request.deadline - request.now()).total_seconds())
            ):
                native = await backend.sessions.create(request.scope, spec, key=key)
        except BaseException:
            await store.advance_operation(
                request.scope, key, "outcome_unknown", now=request.now(), fence=lease
            )
            raise
        await store.advance_operation(
            request.scope,
            key,
            "processed",
            now=request.now(),
            fence=lease,
            resource=native.ref,
            result=native.model_dump(mode="json"),
        )
        return native
    finally:
        await store.release_lease(lease)


async def prepare_openai(request: ProviderPreparationRequest) -> PreparedTurn:
    try:
        return await _prepare_openai(request)
    except UnsupportedCapability as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    except ProviderError as error:
        if error.native_code != "host_delegation_enabled":
            raise
        raise AdmissionDenied(reason="backend_unsupported") from error


async def _prepare_openai(request: ProviderPreparationRequest) -> PreparedTurn:
    revision = request.admission.backend_revision
    runtime = runtime_for(request.deps.turn_runtimes.get(PROFILE), revision)
    if revision is None or not request.deps.channel_backends or request.deps.turn_path != "mux":
        raise AdmissionDenied(reason="backend_unsupported")
    if (
        request.admission.shared_owner is not None
        or revision.thread_mode != "per_caller"
        or request.transfer is not None
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if (
        revision.channel.tenant_id != request.scope.tenant_id
        or revision.channel.platform != request.platform
        or request.scope.account_id != str(request.session_account_id)
    ):
        raise ScopeViolation("preparation", "OpenAI thread differs from admitted scope")
    current = await reauthorize(request.deps, request.admission)
    if (
        current.memory_read_only
        or current.origin_seal_ids
        or current.source_sealed
        or current.asks_before_publishing
        or current.slack_turn_context_id is not None
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    request = replace(request, admission=current)
    # Resolve a host-authorized native plan before creating a private transport.
    spec = await runtime.session_plan(request)
    _check_spec(spec, runtime, request.scope, revision)
    store = request.deps.state_store or PostgresStateStore(request.deps.sessionmaker)
    await store.put_config_revision(revision)
    slot = Slot(
        thread=ThreadRef(channel=revision.channel, thread_id=request.thread_id),
        account_id=request.scope.account_id,
    )
    plan_digest = request_digest(
        {"spec": spec.model_dump(mode="json"), "controls": runtime.controls.model_dump(mode="json")}
    )
    binding = await store.get_binding(slot)
    reused = binding is not None
    if binding is not None and (
        not request.reuse_existing
        or binding.profile != PROFILE
        or binding.provider != "openai"
        or binding.config_revision != revision.local
        or binding.native_refs.get("agent") != spec.agent.id
        or binding.native_refs.get("account_scope") != runtime.account_scope_id
        or binding.native_refs.get("model") != revision.model
        or binding.native_refs.get("plan_digest") != plan_digest
    ):
        raise ContinuityLost(
            binding.id,
            ("bound OpenAI configuration changed; native replacement is not implemented",),
        )
    backend = driver(runtime, revision, request.scope, binding)
    if binding is None:
        native = await _create(request, store, slot, backend, spec, runtime)
        # An acknowledged cached create may predate a process restart. Validate
        # actual provider ownership/model/environment before publishing it.
        verifying = driver(runtime, revision, request.scope, native.binding)
        native = await verifying.sessions.retrieve(request.scope, native.ref)
        if (
            native.ref.provider != "openai"
            or native.ref.account_scope_id != runtime.account_scope_id
            or native.ref.tenant_id != request.scope.tenant_id
            or native.ref.account_id != request.scope.account_id
            or native.binding.native_refs.get("agent") != spec.agent.id
        ):
            raise ScopeViolation(native.ref.id, "OpenAI creation returned a foreign session")
        if native.state == "terminated":
            raise ContinuityLost(native.ref.id, ("OpenAI session terminated during creation",))
        binding = ProviderBinding(
            id=str(uuid5(NAMESPACE_URL, "openai:binding:" + slot.model_dump_json())),
            thread=slot.thread,
            provider="openai",
            profile=PROFILE,
            native_refs={
                **native.binding.native_refs,
                "agent": spec.agent.id,
                "account_scope": runtime.account_scope_id,
                "model": "gpt-6-luna",
                "plan_digest": plan_digest,
            },
            generation=1,
            config_revision=revision.local,
            legacy_account_id=request.scope.account_id,
        )
        binding = await store.put_binding(binding, expected_generation=0)
        backend = driver(runtime, revision, request.scope, binding)
    else:
        ref = ResourceRef(
            id=binding.native_refs["session"],
            kind="session",
            provider="openai",
            account_scope_id=runtime.account_scope_id,
            tenant_id=request.scope.tenant_id,
            account_id=request.scope.account_id,
        )
        native = await backend.sessions.retrieve(request.scope, ref)
        if native.state == "terminated":
            raise ContinuityLost(binding.id, ("OpenAI session terminated",))
    ref = native.ref

    async def record(*, observation: UsageObservation) -> bool:
        if observation.session != ref or observation.thread_id is not None:
            raise ScopeViolation(ref.id, "OpenAI host usage differs from bound root session")
        return await record_provider_usage(
            sessionmaker=request.deps.sessionmaker,
            binding_id=binding.id,
            observation=observation,
            tenant_id=request.tenant_id,
            platform_user_id=request.external_user_id,
            provider_price=runtime.price,
            infrastructure_usd=await runtime.infrastructure(observation),
            billing_grain="turn",
            managed_session_id=ref.id,
            model_id="gpt-6-luna",
            markup=request.deps.markup,
            channel_id=revision.channel.channel_id,
        )

    return PreparedTurn(
        admission=request.admission,
        ma_session_id=ref.id,
        mapping_id=None,
        watermark=None,
        reused=reused,
        session_account_id=request.session_account_id,
        _record=record,
        backend=backend,
        session_ref=ref,
    )


_registered = False


def register_host() -> None:
    """Idempotent startup registration with no transport or credential discovery."""
    global _registered
    if _registered:
        return
    register_turn_backend(PROFILE, _backend)
    register_turn_codec(PROFILE, _codec)
    register_turn_preparation(PROFILE, prepare_openai)
    _registered = True


register_host()
