"""Explicit Gemini deployment and caller-private binding for the host turn seam.

A runtime must supply the exact admitted deployment and durable stores. No
Anthropic agent is translated, and no credentials or clients are discovered.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from daimon.core.turn.admission import reauthorize
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiJournal,
    GeminiUsageRuntime,
    gemini_backend,
)
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
from daimon.core.usage_recording import record_provider_usage
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, ThreadRef
from mux.contracts.resources import SessionSpec
from mux.contracts.usage import UsageObservation
from mux.errors import BindingConflict, ScopeViolation, UnsupportedCapability
from mux.state.lease import Slot


async def prepare_gemini_turn(request: ProviderPreparationRequest) -> PreparedTurn:
    try:
        return await _prepare_gemini_turn(request)
    except UnsupportedCapability as err:
        raise AdmissionDenied(reason="backend_unsupported") from err


async def _prepare_gemini_turn(request: ProviderPreparationRequest) -> PreparedTurn:
    admission = await reauthorize(request.deps, request.admission)
    config = admission.backend_revision
    runtime = request.deps.turn_runtimes.get(PROFILE)
    if config is None or runtime is None or config.profile != PROFILE:
        raise UnsupportedCapability(("gemini_explicit_runtime",), PROFILE)
    if (
        config.channel.tenant_id != request.scope.tenant_id
        or config.channel.platform != request.platform
        or str(admission.account_id) != request.scope.account_id
        or str(request.tenant_id) != request.scope.tenant_id
        or request.session_account_id != admission.account_id
    ):
        raise ScopeViolation(request.thread_id, "Gemini preparation differs from admission")
    # Shared ownership, seals, mounts and transfer require a separate complete
    # deployment adapter. Never silently drop those obligations in this slice.
    if (
        config.thread_mode != "per_caller"
        or admission.shared_owner is not None
        or admission.origin_seal_ids
        or admission.memory_read_only
        or admission.asks_before_publishing
        or admission.channel_skills
        or request.transfer is not None
        or not request.reuse_existing
    ):
        raise UnsupportedCapability(("gemini_host_deployment_policy",), PROFILE)
    if not isinstance(runtime.journal, GeminiJournal) or not isinstance(
        runtime.usage_store, GeminiUsageRuntime
    ):
        raise UnsupportedCapability(("gemini_durable_runtime",), PROFILE)
    journal, usage = runtime.journal, runtime.usage_store
    deployment = journal.deployment
    if (
        deployment is None
        or deployment.config_digest != config.digest
        or deployment.config_revision != config.local
        or deployment.agent.model.provider != "gemini"
        or deployment.agent.model.id != config.model
        or request.deps.state_store is not journal.state_store
    ):
        raise UnsupportedCapability(("gemini_pinned_deployment",), PROFILE)
    thread = ThreadRef(channel=config.channel, thread_id=request.thread_id)
    slot = Slot(thread=thread, account_id=request.scope.account_id)
    store = journal.state_store
    prior = await store.get_binding(slot)
    binding_id = str(uuid5(NAMESPACE_URL, f"gemini.host:{slot.model_dump_json()}"))
    if prior is not None and (
        prior.id != binding_id
        or prior.provider != "gemini"
        or prior.profile != PROFILE
        or prior.config_revision != config.local
        or "session" not in prior.native_refs
    ):
        raise ScopeViolation(binding_id, "Gemini binding differs from admitted revision")
    backend = gemini_backend(config, request.scope, runtime, deployment.account_scope_id)
    if prior is None:
        agent = await backend.agents.create(
            request.scope, deployment.agent, key=f"{binding_id}:agent"
        )
        environment = await backend.environments.create(
            request.scope,
            deployment.environment,
            key=f"{binding_id}:environment",
        )
        session = await backend.sessions.create(
            request.scope,
            SessionSpec(
                agent=agent.ref,
                agent_revision=agent.revision,
                environment=environment.ref,
                config_revision=config.local,
                extensions={
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={"binding_id": binding_id, "thread": thread.model_dump(mode="json")},
                    )
                },
            ),
            key=f"{binding_id}:session",
        )
        binding = session.binding.model_copy(update={"legacy_account_id": request.scope.account_id})
        try:
            await store.put_binding(binding, expected_generation=0)
        except BindingConflict:
            winner = await store.get_binding(slot)
            if winner != binding:
                raise
        session_ref = session.ref
    else:
        session_ref = ResourceRef(
            id=prior.native_refs["session"],
            kind="session",
            provider="gemini",
            account_scope_id=deployment.account_scope_id,
            tenant_id=request.scope.tenant_id,
            account_id=request.scope.account_id,
        )
        session = await backend.sessions.retrieve(request.scope, session_ref)
        if (
            session.binding.id != prior.id
            or session.binding.thread != prior.thread
            or session.binding.config_revision != prior.config_revision
        ):
            raise ScopeViolation(session_ref.id, "Gemini session differs from persisted binding")
    session_ref = session.ref
    current = await reauthorize(request.deps, admission)
    if (
        current.origin_seal_ids != admission.origin_seal_ids
        or current.memory_read_only != admission.memory_read_only
        or current.asks_before_publishing != admission.asks_before_publishing
        or current.backend_revision != config
    ):
        raise UnsupportedCapability(("gemini_changed_admission",), PROFILE)

    async def record(*, observation: UsageObservation) -> bool:
        if observation.session != session_ref or observation.model is None:
            raise ScopeViolation(observation.id, "Gemini billing received foreign usage")
        # A moving alias has no verified resolved-price identity in this slice.
        # Preserve its counts in the outbox; never settle it using a guessed tariff.
        price = (
            None
            if observation.model.id == "gemini-flash-latest"
            else usage.prices.get(observation.model.id)
        )
        return await record_provider_usage(
            sessionmaker=request.deps.sessionmaker,
            binding_id=binding_id,
            observation=observation,
            tenant_id=request.tenant_id,
            platform_user_id=request.external_user_id,
            managed_session_id=session_ref.id,
            provider_price=price,
            infrastructure_usd=usage.infrastructure_usd,
            billing_grain="turn",
            markup=request.deps.markup,
            channel_id=current.budget_channel_id or current.origin_channel_id,
        )

    return PreparedTurn(
        admission=replace(current, backend_revision=config),
        ma_session_id=session_ref.id,
        mapping_id=None,
        watermark=None,
        reused=prior is not None,
        session_account_id=request.session_account_id,
        _record=record,
        backend=backend,
        session_ref=session_ref,
    )
