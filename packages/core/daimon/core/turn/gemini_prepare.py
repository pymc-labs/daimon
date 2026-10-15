"""Explicit Gemini deployment and caller-private binding for the host turn seam.

A runtime must supply the exact admitted deployment and durable stores. No
Anthropic agent is translated, and no credentials or clients are discovered.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from daimon.core.native_footer_cost import NativeFooterCostContext
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
        or (
            admission.memory_read_only
            and (request.worker is None or "memory" not in request.worker.declared_drops)
        )
        or admission.asks_before_publishing
        or (
            admission.channel_skills
            and (request.worker is None or "skills" not in request.worker.declared_drops)
        )
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
    if request.worker is not None or admission.backend_revision_origin == "agent":
        from daimon.core.turn.gemini_deployment import load_gemini_deployment

        if request.worker is None or request.counterpart is None or request.source is None:
            raise AdmissionDenied(reason="backend_unsupported")
        plan = load_gemini_deployment(request)
        expected = plan.worker_deployment(request.worker, config)
        checked, ref = request.counterpart, request.counterpart.agent.ref
        if (
            deployment is None
            or replace(deployment, native_agent=None) != expected
            or checked.agent.spec != expected.agent
            or (
                not plan.worker_materialization
                and (
                    checked.catalog_id != plan.counterpart_catalog_id
                    or checked.local_revision != plan.counterpart_catalog_revision
                    or ref.id != plan.agent_id
                )
            )
            or (ref.provider, ref.kind, ref.tenant_id, ref.account_id, ref.account_scope_id)
            != ("gemini", "agent", request.scope.tenant_id, request.scope.account_id, plan.project)
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        if plan.worker_materialization and request.verified_counterpart != checked:
            raise AdmissionDenied(reason="backend_unsupported")
        deployment = replace(deployment, native_agent=request.counterpart.agent)
        journal = replace(journal, deployment=deployment)
        runtime = replace(runtime, journal=journal)
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
        or (
            deployment.native_agent is not None
            and prior.native_refs.get("agent") != deployment.native_agent.ref.id
        )
    ):
        raise ScopeViolation(binding_id, "Gemini binding differs from admitted revision")
    backend = gemini_backend(config, request.scope, runtime, deployment.account_scope_id)
    if prior is None:
        agent = deployment.native_agent
        if agent is None:
            agent = await backend.agents.create(
                request.scope, deployment.agent, key=f"{binding_id}:agent"
            )
        environment = (
            await backend.environments.create(
                request.scope, deployment.environment, key=f"{binding_id}:environment"
            )
            if deployment.max_output_tokens is None
            else None
        )
        session = await backend.sessions.create(
            request.scope,
            SessionSpec(
                agent=agent.ref,
                agent_revision=agent.revision,
                environment=environment.ref if environment is not None else None,
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
        binding = prior
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

    footer_cost = (
        NativeFooterCostContext(
            request.deps.sessionmaker,
            request.scope,
            binding,
            session_ref,
            request.deps.markup,
        )
        if request.deps.native_footer_cost
        else None
    )

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
        settled = await record_provider_usage(
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

        if footer_cost is not None:
            footer_cost.note(
                observation,
                price=price,
                infrastructure_usd=usage.infrastructure_usd,
                settled=settled,
            )
        return settled

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
        native_footer_cost=footer_cost,
        runtime=runtime if request.worker is not None else None,
    )
