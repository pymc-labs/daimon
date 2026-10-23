"""Explicit OpenAI host composition; no SDK/key discovery or default selection.

Operators inject an authorized native session plan and private driver transport.
The host publishes its actual binding before running. `TurnPersistence` owns
the lease, send claims and journal. Unknown container cost remains durable pending usage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import (
    TurnBackend,
    TurnBackendRequest,
    TurnRuntime,
    register_turn_backend,
)
from daimon.core.native_footer_cost import NativeFooterCostContext
from daimon.core.pricing import ProviderPrice
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import reauthorize
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.io import TurnCodecRequest, TurnIO, register_turn_codec
from daimon.core.turn.openai_codec import CONVERSATION_PROFILE, PROFILE, PROFILES
from daimon.core.turn.openai_io import OpenAITurnIO
from daimon.core.turn.openai_state import (
    OpenAIRecoveryJournal,
    OpenAIUsageRevisions,
    initial_input_consumed,
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
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import ProviderBinding, Session, SessionSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.openai import (
    Authorization,
    MCPSecretResolver,
    OpenAIDriver,
    SessionControls,
    Transport,
)
from mux.errors import ContinuityLost, ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles.openai import NATIVE_MODELS
from mux.state.lease import Slot
from mux.state.operations import request_digest
from mux.state.store import ERA_REF, StateStore, adopted_identity

#: HTTP answers that reject a session create outright: no session exists afterwards.
#: A 409 or 429 may follow an accepted create, so those stay `outcome_unknown`.
_DEFINITE_REJECTIONS = frozenset({"400", "401", "403", "404", "422"})


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
    mcp_secrets: MCPSecretResolver | None = field(default=None, kw_only=True)
    price: ProviderPrice | None = None
    infrastructure: Callable[[UsageObservation], Awaitable[Decimal | None]] = unknown_infrastructure


def runtime_for(runtime: TurnRuntime | None, revision: ConfigRevision | None) -> OpenAIHostRuntime:
    # Validate before transport_factory can access credentials or make requests.
    if not isinstance(runtime, OpenAIHostRuntime) or revision is None:
        raise UnsupportedCapability(("openai_host_runtime",), PROFILE)
    if (
        revision.profile not in PROFILES
        or revision.backend != "openai"
        or revision.model not in NATIVE_MODELS
    ):
        raise UnsupportedCapability(("openai_host_model",), PROFILE)
    if (
        not runtime.account_scope_id.strip()
        or runtime.controls.model != revision.model
        or runtime.controls.multi_agent_enabled is not False
        or runtime.controls.container_size != ("small" if revision.profile == PROFILE else None)
        or not isinstance(runtime.journal, OpenAIRecoveryJournal)
        or not isinstance(runtime.usage_store, OpenAIUsageRevisions)
    ):
        raise UnsupportedCapability(("openai_host_controls_and_durable_state",), PROFILE)
    if runtime.price is not None and (
        runtime.price.provider != "openai" or runtime.price.model != revision.model
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
        profile_id=revision.profile,
        journal=cast(OpenAIRecoveryJournal, runtime.journal),
        usage_revisions=cast(OpenAIUsageRevisions, runtime.usage_store),
        authorization=runtime.authorization,
        binding_lookup=lookup,
        session_controls=runtime.controls,
        mcp_secrets=runtime.mcp_secrets,
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
        initial_input_loader=initial_input_consumed,
    )


def _check_spec(
    spec: SessionSpec, runtime: OpenAIHostRuntime, scope: Scope, revision: ConfigRevision
) -> None:
    if revision.profile == CONVERSATION_PROFILE and (
        spec.environment is not None or spec.resources or spec.extensions
    ):
        raise UnsupportedCapability(("conversation_only_workspace",), revision.profile)
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
    if request.worker is not None or request.native_budget_usd is not None:
        active = current_persistence.get()
        if (
            active is None
            or active.store is not store
            or active.scope != request.scope
            or active.slot != slot
            or request.before_native is None
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        active.check_session(request.scope, None)

        initial = (
            request.initial_input
            if backend.capabilities().profile_id == CONVERSATION_PROFILE
            else None
        )
        if backend.capabilities().profile_id == CONVERSATION_PROFILE and initial is None:
            raise AdmissionDenied(reason="backend_unsupported")

        async def create(native_key: str) -> Session:
            native = await backend.sessions.create(
                request.scope, spec, key=native_key, initial=initial
            )
            if initial is not None:
                native = native.model_copy(
                    update={
                        "binding": native.binding.model_copy(
                            update={
                                "native_refs": {
                                    **native.binding.native_refs,
                                    "initial_input_operation": native_key,
                                    "initial_input_turn": active.operation_key,
                                    "initial_input_digest": request_digest(
                                        initial.model_dump(mode="json")
                                    ),
                                }
                            }
                        )
                    }
                )
            return native

        return await active.mutate(
            None,
            "send" if initial is not None else "session_create",
            {
                "spec": spec.model_dump(mode="json"),
                "controls": runtime.controls.model_dump(mode="json"),
                **({"initial": initial.model_dump(mode="json")} if initial is not None else {}),
            },
            create,
            Session,
            lambda _: "processed",
            guard=request.before_native,
            retry_failed=False,
        )
    # Stable per empty slot, including after a crash. Changed config/plan gets a
    # digest conflict, not a fresh operation which might create a second session.
    # A released slot's era suffix keeps an earlier era's creation from replaying.
    _, era = adopted_identity(await store.get_binding(slot, include_released=True), "")
    key = await _create_key(
        store,
        request.scope,
        "openai:prepare:" + str(uuid5(NAMESPACE_URL, slot.model_dump_json())) + era,
    )
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
        except ProviderError as error:
            # A definite rejection created no session: the slot's next create gets a
            # fresh key (`_create_key`), even with a changed plan. Anything else may have
            # reached OpenAI and is never resent blindly.
            rejected = error.native_code in _DEFINITE_REJECTIONS
            await store.advance_operation(
                request.scope,
                key,
                "failed" if rejected else "outcome_unknown",
                now=request.now(),
                fence=lease,
                result={"rejected": True} if rejected else None,
            )
            raise
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


async def _create_key(store: StateStore, scope: Scope, base: str) -> str:
    """The slot's create key: past each create OpenAI rejected outright, a fresh one.

    A rejected create (`failed`, `rejected`) proves no session exists, so a new key
    may send again; any other recorded state keeps its key and its digest check.
    """
    key, attempt = base, 0
    while (record := await store.get_operation(scope, key)) is not None and (
        record.operation.status == "failed" and record.result.get("rejected") is True
    ):
        attempt += 1
        key = f"{base}:rejected-{attempt}"
    return key


async def prepare_openai(request: ProviderPreparationRequest) -> PreparedTurn:
    try:
        return await _prepare_openai(request)
    except UnsupportedCapability as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    except ProviderError as error:
        if error.native_code != "host_delegation_enabled":
            raise
        raise AdmissionDenied(reason="backend_unsupported") from error


async def _check_daily_model_cap(
    request: ProviderPreparationRequest, runtime: OpenAIHostRuntime, revision: ConfigRevision
) -> None:
    """Refuse once the tenant's daily spend on this model reaches its cap.

    OpenAI sessions carry no native spend limit on this project, so this is the
    guard, checked before any native call of an agent turn or a fleet worker. A
    worker's own reservation already counts toward today's spend, so it may use
    the cap up exactly; an agent turn starts only while some of it remains.
    """
    from daimon.core.stores.openai_spend import openai_spend_today

    cap = request.deps.openai_daily_model_cap_usd
    if cap is None or revision.model is None:
        return
    async with request.deps.sessionmaker() as session:
        spent = await openai_spend_today(
            session,
            tenant_id=request.tenant_id,
            provider=revision.backend,
            model=revision.model,
            now=request.now(),
            rates=runtime.price,
            markup=request.deps.markup,
        )
    over = spent > cap if request.worker is not None else spent >= cap
    if over:
        import structlog

        structlog.get_logger(__name__).warning(
            "turn.daily_model_cap_reached",
            tenant_id=str(request.tenant_id),
            model=revision.model,
            cap_usd=str(cap),
        )
        raise AdmissionDenied(reason="provider_daily_cap_reached", provider=revision.model)


async def _prepare_openai(request: ProviderPreparationRequest) -> PreparedTurn:
    revision = request.admission.backend_revision
    runtime = runtime_for(
        request.deps.turn_runtimes.get(revision.profile) if revision is not None else None, revision
    )
    if revision is None or not request.deps.channel_backends or request.deps.turn_path != "mux":
        raise AdmissionDenied(reason="backend_unsupported")
    if (
        request.admission.shared_owner is not None
        or request.admission.hold is not None
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
    if revision.profile == CONVERSATION_PROFILE and request.worker is None:
        raise AdmissionDenied(reason="backend_unsupported")
    current = await reauthorize(request.deps, request.admission)
    if (
        (
            current.memory_read_only
            and not (
                revision.profile == CONVERSATION_PROFILE
                and request.worker is not None
                and "memory" in request.worker.declared_drops
                and not request.worker.resources
            )
        )
        or current.origin_seal_ids
        or current.source_sealed
        or current.asks_before_publishing
        or current.slack_turn_context_id is not None
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if current.daimon_rules_need_driver:
        # Daimon tool rules need an approval pause this backend does not have.
        # Refused before any plan, key or transport is touched.
        raise AdmissionDenied(reason="backend_unsupported") from UnsupportedCapability(
            ("daimon_tool_rules",), revision.profile
        )
    request = replace(request, admission=current)
    await _check_daily_model_cap(request, runtime, revision)
    # Resolve a host-authorized native plan before creating a private transport.
    spec = await runtime.session_plan(request)
    _check_spec(spec, runtime, request.scope, revision)
    store = request.deps.state_store or PostgresStateStore(request.deps.sessionmaker)
    if request.worker is None and current.backend_revision_origin == "agent":
        # Admission retained this exact source consent; a generic StateStore
        # write has no agent-origin association and cannot recreate that proof.
        async with request.deps.sessionmaker.begin() as session:
            retained = await mux_state.get_config_agent_revision(
                session,
                revision,
                agent_id=derive_agent_uuid(
                    tenant_id=request.tenant_id, ma_agent_id=current.agent.id
                ),
            )
            if retained is None:
                raise ScopeViolation(request.thread_id, "missing original backend consent")
            await mux_state.put_config_revision(
                session, revision, origin="agent", agent_revision=retained
            )
    else:
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
        or binding.profile != revision.profile
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
        try:
            verifying.sessions.validate_spend_control(native)
            native = await verifying.sessions.retrieve(request.scope, native.ref)
        except (ContinuityLost, ProviderError, ScopeViolation):
            if runtime.controls.require_spend_control_echo:
                from daimon.core.turn.run import (
                    _archive_orphaned_session,  # pyright: ignore[reportPrivateUsage]
                )

                async def cleanup() -> DeletionReceipt:
                    active = current_persistence.get()
                    if active is not None:
                        return await active.mutate(
                            None,
                            "session_delete",
                            {"session": native.ref.model_dump(mode="json")},
                            lambda key: verifying.sessions.delete(
                                request.scope, native.ref, key=key
                            ),
                            DeletionReceipt,
                            lambda _: "processed",
                            retry_failed=False,
                        )
                    return await verifying.sessions.delete(
                        request.scope,
                        native.ref,
                        key=f"openai:rejected:{native.ref.id}",
                    )

                await _archive_orphaned_session(
                    request.deps.anthropic,
                    session_id=native.ref.id,
                    archive=cleanup,
                )
            raise
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
        binding_id, era = adopted_identity(
            await store.get_binding(slot, include_released=True),
            str(uuid5(NAMESPACE_URL, "openai:binding:" + slot.model_dump_json())),
        )
        binding = ProviderBinding(
            id=binding_id,
            thread=slot.thread,
            provider="openai",
            profile=revision.profile,
            native_refs={
                **native.binding.native_refs,
                "agent": spec.agent.id,
                "account_scope": runtime.account_scope_id,
                "model": revision.model or "",
                "plan_digest": plan_digest,
                **({ERA_REF: era} if era else {}),
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
    footer_cost = (
        NativeFooterCostContext(
            request.deps.sessionmaker, request.scope, binding, ref, request.deps.markup
        )
        if request.deps.native_footer_cost
        else None
    )

    # runtime_for admitted exactly one native model; usage is priced at its rate.
    model_id = revision.model or ""

    async def record(*, observation: UsageObservation) -> bool:
        if observation.session != ref or observation.thread_id is not None:
            raise ScopeViolation(ref.id, "OpenAI host usage differs from bound root session")
        infrastructure = await runtime.infrastructure(observation)
        settled = await record_provider_usage(
            sessionmaker=request.deps.sessionmaker,
            binding_id=binding.id,
            observation=observation,
            tenant_id=request.tenant_id,
            platform_user_id=request.external_user_id,
            provider_price=runtime.price,
            infrastructure_usd=infrastructure,
            billing_grain="turn",
            managed_session_id=ref.id,
            model_id=model_id,
            markup=request.deps.markup,
            channel_id=revision.channel.channel_id,
        )

        if footer_cost is not None:
            from mux.contracts.ids import ModelRef

            footer_cost.note(
                observation.model_copy(update={"model": ModelRef(provider="openai", id=model_id)}),
                price=runtime.price,
                infrastructure_usd=infrastructure,
                settled=settled,
            )
        return settled

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
        native_footer_cost=footer_cost,
    )


def register_host() -> None:
    """Fill missing host callbacks while preserving explicit application overrides."""
    for profile in PROFILES:
        register_turn_backend(profile, _backend, fallback=True)
        register_turn_codec(profile, _codec, fallback=True)
        register_turn_preparation(profile, prepare_openai, fallback=True)


register_host()
