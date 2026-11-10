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

import structlog
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import native_mcp_session
from daimon.core.mux_backend import (
    TurnBackend,
    TurnBackendRequest,
    TurnRuntime,
    register_turn_backend,
)
from daimon.core.native_footer_cost import NativeFooterCostContext
from daimon.core.pricing import ProviderPrice
from daimon.core.session_compat import ReplaceSession
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import reauthorize
from daimon.core.turn.carry_over import carry_on_adoption, register_session_reader
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.io import TurnCodecRequest, TurnIO, register_turn_codec
from daimon.core.turn.openai_codec import PROFILE, PROFILES
from daimon.core.turn.openai_io import OpenAITurnIO
from daimon.core.turn.openai_state import (
    OpenAIRecoveryJournal,
    OpenAIUsageRevisions,
    invocation_baseline,
)
from daimon.core.turn.persistence import UncertainSend, current_persistence
from daimon.core.turn.prepare import (
    CONTINUED,
    PreparedTurn,
    ProviderPreparationRequest,
    register_turn_preparation,
)
from daimon.core.turn.replace_binding import (
    BindingFacts,
    OldSession,
    binding_decision,
    recorded_facts,
    replace_binding,
)
from daimon.core.usage_recording import record_provider_usage
from mux.contracts.config import ConfigRevision
from mux.contracts.digest import digest_body
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding, ResourceBinding, Session, SessionSpec
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
from mux.state.lease import Lease, Slot
from mux.state.operations import request_digest
from mux.state.store import ERA_REF, StateStore, adopted_identity

#: HTTP answers that reject a session create outright: no session exists afterwards.
#: A 409 or 429 may follow an accepted create, so those stay `outcome_unknown`.
# Profiles whose sessions mount uploaded files, so a replacement can carry them.
_MOUNTING_PROFILES = frozenset({"openai.persistent_workspace"})
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

    # Each turn rewrites the session's MCP bearer in place. It keeps the slot grant
    # the session was created with, so the bearer matches the grant the executing
    # driver binds to its origin; it never takes the origin's own id.
    grant = binding.native_refs.get("daimon_chat_session_id") if binding is not None else None
    mcp_secrets = runtime.mcp_secrets
    if mcp_secrets is not None and grant is not None:
        resolve_mcp = mcp_secrets

        async def granted(authorized: Scope, reference: str, destination: str) -> str:
            with native_mcp_session(scope, grant):
                return await resolve_mcp(authorized, reference, destination)

        mcp_secrets = granted

    return OpenAIDriver(
        cast(Transport, runtime.transport_factory(revision, scope)),
        account_scope_id=runtime.account_scope_id,
        profile_id=revision.profile,
        journal=cast(OpenAIRecoveryJournal, runtime.journal),
        usage_revisions=cast(OpenAIUsageRevisions, runtime.usage_store),
        authorization=runtime.authorization,
        binding_lookup=lookup,
        session_controls=runtime.controls,
        mcp_secrets=mcp_secrets,
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
    *,
    replacing: tuple[ProviderBinding, str, Lease] | None = None,
) -> Session:
    # Stable per empty slot, including after a crash. Changed config/plan gets a
    # digest conflict, not a fresh operation which might create a second session.
    # A released slot's era suffix keeps an earlier era's creation from replaying,
    # as a replacement's does (`replace_binding`, which already holds the lease).
    if replacing is None:
        _, era = adopted_identity(await store.get_binding(slot, include_released=True), "")
    else:
        _, era, _ = replacing
    key = await _create_key(
        store,
        request.scope,
        "openai:prepare:" + str(uuid5(NAMESPACE_URL, slot.model_dump_json())) + era,
    )
    digest = request_digest({"spec": digest_body(spec), "controls": digest_body(runtime.controls)})
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
    lease = (
        await store.acquire_lease(
            slot, holder=str(uuid4()), turn_id=key, now=request.now(), ttl=timedelta(minutes=5)
        )
        if replacing is None
        else replacing[2]
    )
    try:
        # The lease winner rechecks after acquisition before claiming or creating.
        bound = await store.get_binding(slot)
        if (None if bound is None else (bound.id, bound.generation)) != (
            None if replacing is None else (replacing[0].id, replacing[0].generation)
        ):
            raise ScopeViolation("preparation", "OpenAI slot was bound during preparation")
        final = await reauthorize(request.deps, request.admission)
        if final != request.admission:
            raise AdmissionDenied(reason="backend_unsupported")
        await store.claim_send(request.scope, key, now=request.now(), fence=lease)
        try:
            # Bound this create call within the lease and the turn deadline.
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
        if replacing is None:
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
    """OpenAI sessions carry no native spend limit on this project: the daily model cap."""
    from daimon.core.turn.daily_model_cap import check_daily_model_cap

    await check_daily_model_cap(
        request,
        provider=revision.backend,
        model=revision.model,
        price=runtime.price,
        cap=request.deps.openai_daily_model_cap_usd,
    )


async def _prepare_openai(request: ProviderPreparationRequest) -> PreparedTurn:
    revision = request.admission.backend_revision
    runtime = runtime_for(
        request.deps.turn_runtimes.get(revision.profile) if revision is not None else None, revision
    )
    try:
        return await _prepare_with_runtime(request, runtime)
    except BaseException:
        # A per-turn MCP credential minted before the turn existed (a template's
        # session create) is revoked now, not left to its TTL.
        if runtime.end_of_turn is not None:
            try:
                await runtime.end_of_turn()
            except Exception:
                structlog.get_logger(__name__).warning(
                    "turn.mcp_credential_revoke_failed", exc_info=True
                )
        raise


async def _prepare_with_runtime(
    request: ProviderPreparationRequest, runtime: OpenAIHostRuntime
) -> PreparedTurn:
    revision = request.admission.backend_revision
    if revision is None or not request.deps.channel_backends or request.deps.turn_path != "mux":
        raise AdmissionDenied(reason="backend_unsupported")
    # A held turn runs on a per-turn held credential (`template_mcp`). A shared
    # thread binds the account-less slot and runs with no caller MCP credential;
    # an agent-origin source's counterpart and MCP are per caller.
    shared = request.admission.shared_owner
    if shared is not None and request.admission.backend_revision_origin == "agent":
        raise AdmissionDenied(reason="backend_unsupported")
    if (
        revision.channel.tenant_id != request.scope.tenant_id
        or revision.channel.platform != request.platform
        or str(request.session_account_id)
        != (str(shared) if shared is not None else request.scope.account_id)
        or (shared is not None and runtime.mcp_secrets is not None)
    ):
        raise ScopeViolation("preparation", "OpenAI thread differs from admitted scope")
    current = await reauthorize(request.deps, request.admission)
    # A read-only turn's origin is stamped read-only (`turn.run`). A sealed turn's
    # transcript stamp has no provider representation yet.
    if (
        current.origin_seal_ids
        or current.source_sealed
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
    if current.backend_revision_origin == "agent":
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
        account_id=None if shared is not None else request.scope.account_id,
    )
    # OpenAI captures MCP bearers during creation, before an origin is bound.
    # A stable slot grant survives reuse; only the executing driver binds it to
    # an origin. Including it in the spec digest rejects ungranted old receipts.
    chat_session_id = (
        str(uuid5(NAMESPACE_URL, "openai:mcp-session:" + slot.model_dump_json()))
        if runtime.mcp_secrets is not None
        else None
    )
    if chat_session_id is not None:
        spec = spec.model_copy(
            update={"metadata": {**spec.metadata, "daimon_chat_session_id": chat_session_id}}
        )
    # A shared session's plan is the same for every caller: its refs' account is not.
    planned_spec = (
        spec
        if shared is None
        else spec.model_copy(
            update={
                "agent": spec.agent.model_copy(update={"account_id": None}),
                "environment": spec.environment.model_copy(update={"account_id": None})
                if spec.environment is not None
                else None,
            }
        )
    )
    plan_digest = request_digest(
        {"spec": digest_body(planned_spec), "controls": digest_body(runtime.controls)}
    )
    binding = await store.get_binding(slot)
    reused = binding is not None
    continuity = CONTINUED

    async def publishable(
        native: Session, binding_id: str, era: str
    ) -> tuple[Session, ProviderBinding]:
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
        return native, ProviderBinding(
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
                **(
                    {"daimon_chat_session_id": chat_session_id}
                    if chat_session_id is not None
                    else {}
                ),
                **({ERA_REF: era} if era else {}),
            },
            generation=1,
            config_revision=revision.local,
            legacy_account_id=None if shared is not None else request.scope.account_id,
        )

    def session_ref(bound: ProviderBinding, account_scope_id: str) -> ResourceRef:
        return ResourceRef(
            id=bound.native_refs["session"],
            kind="session",
            provider="openai",
            account_scope_id=account_scope_id,
            tenant_id=request.scope.tenant_id,
            account_id=request.scope.account_id,
        )

    desired = BindingFacts(
        profile=revision.profile,
        provider="openai",
        config_revision=revision.local,
        agent=spec.agent.id,
        account_scope=runtime.account_scope_id,
        model=revision.model or "",
        plan_digest=plan_digest,
    )
    decision = (
        None
        if binding is None
        else binding_decision(
            recorded_facts(binding),
            desired,
            reuse_existing=request.reuse_existing and request.transfer is None,
        )
    )
    if binding is not None and isinstance(decision, ReplaceSession):
        old = binding
        # A session of another provider or account scope is not read with this one's driver.
        readable = (
            old.provider == "openai"
            and "session" in old.native_refs
            and old.native_refs.get("account_scope") == runtime.account_scope_id
        )
        target = driver(runtime, revision, request.scope)

        async def successor(
            era: str, resources: tuple[ResourceBinding, ...], lease: Lease
        ) -> ProviderBinding:
            planned = (
                spec.model_copy(update={"resources": (*spec.resources, *resources)})
                if resources
                else spec
            )
            with native_mcp_session(request.scope, chat_session_id):
                created = await _create(
                    request, store, slot, target, planned, runtime, replacing=(old, era, lease)
                )
            verified, published = await publishable(created, old.id, era)
            successors.append(verified)
            return published

        successors: list[Session] = []

        replaced = await replace_binding(
            store=store,
            slot=slot,
            scope=request.scope,
            old=OldSession(
                binding=old,
                ref=session_ref(old, runtime.account_scope_id) if readable else None,
                backend=driver(runtime, revision, request.scope, old) if readable else None,
            ),
            reasons=decision.reasons,
            create=successor,
            now=request.now(),
            model_id=revision.model or "",
            agent_name=current.agent.name,
            target_artifacts=target.artifacts if revision.profile in _MOUNTING_PROFILES else None,
        )
        binding, continuity, native = replaced.binding, replaced.continuity, successors[-1]
        backend = driver(runtime, revision, request.scope, binding)
    elif binding is None:
        backend = driver(runtime, revision, request.scope)
        carried = await carry_on_adoption(
            request,
            store=store,
            slot=slot,
            model_id=revision.model or "",
            agent_name=current.agent.name,
            target_artifacts=backend.artifacts if revision.profile in _MOUNTING_PROFILES else None,
        )
        planned = spec
        if carried is not None:
            continuity = carried.continuity
            if carried.mounts:
                planned = spec.model_copy(update={"resources": (*spec.resources, *carried.mounts)})
        with native_mcp_session(request.scope, chat_session_id):
            created = await _create(request, store, slot, backend, planned, runtime)
        binding_id, era = adopted_identity(
            await store.get_binding(slot, include_released=True),
            str(shared)
            if shared is not None
            else str(uuid5(NAMESPACE_URL, "openai:binding:" + slot.model_dump_json())),
        )
        native, published = await publishable(created, binding_id, era)
        binding = await store.put_binding(published, expected_generation=0)
        backend = driver(runtime, revision, request.scope, binding)
    else:
        backend = driver(runtime, revision, request.scope, binding)
        native = await backend.sessions.retrieve(
            request.scope, session_ref(binding, runtime.account_scope_id)
        )
        if native.state == "terminated":
            raise ContinuityLost(binding.id, ("OpenAI session terminated",))
    ref = native.ref
    footer_cost = (
        NativeFooterCostContext(
            request.deps.sessionmaker, request.scope, binding, ref, request.deps.markup
        )
        if request.admission.shown("native_footer_cost", request.platform)
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
            agent_id=current.agent.id,
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
        native_chat_session_id=binding.native_refs.get("daimon_chat_session_id"),
        continuity=continuity,
        # Revokes a template turn's per-turn MCP credential however the turn ends.
        _finalize=runtime.end_of_turn,
    )


def _old_session(
    runtime: TurnRuntime,
    revision: ConfigRevision,
    scope: Scope,
    binding: ProviderBinding,
    store: StateStore,
) -> OldSession:
    """A released OpenAI session, read after its thread moved to another provider."""
    host = runtime_for(runtime, revision)
    if binding.native_refs.get("account_scope", host.account_scope_id) != host.account_scope_id:
        raise ScopeViolation(binding.id, "OpenAI session belongs to another account scope")
    ref = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider="openai",
        account_scope_id=host.account_scope_id,
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    return OldSession(binding=binding, ref=ref, backend=driver(host, revision, scope, binding))


def register_host() -> None:
    """Fill missing host callbacks while preserving explicit application overrides."""
    for profile in PROFILES:
        register_session_reader(profile, _old_session)
        register_turn_backend(profile, _backend, fallback=True)
        register_turn_codec(profile, _codec, fallback=True)
        register_turn_preparation(profile, prepare_openai, fallback=True)


register_host()
