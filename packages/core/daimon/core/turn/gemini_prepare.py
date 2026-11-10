"""Explicit Gemini deployment and caller-private binding for the host turn seam.

A runtime supplies the exact admitted deployment and durable stores. Ordinary
source targets require confirmed losses and native readback; credentials and
clients are never discovered implicitly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import structlog
from daimon.core.mux_backend import TurnRuntime
from daimon.core.native_footer_cost import NativeFooterCostContext
from daimon.core.pricing import ProviderPrice, provider_cost_of
from daimon.core.session_compat import ReplaceSession
from daimon.core.turn.admission import reauthorize
from daimon.core.turn.carry_over import carry_on_adoption
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import PROFILE, GeminiJournal, GeminiUsageRuntime, gemini_backend
from daimon.core.turn.gemini_state import HoldSettlement, interaction_of
from daimon.core.turn.prepare import CONTINUED, PreparedTurn, ProviderPreparationRequest
from daimon.core.turn.replace_binding import (
    BindingFacts,
    OldSession,
    binding_decision,
    recorded_facts,
    replace_binding,
)
from daimon.core.usage_recording import record_provider_usage
from mux.contracts.config import ConfigRevision
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding, SessionSpec
from mux.contracts.usage import UsageObservation
from mux.errors import BindingConflict, ScopeViolation, UnsupportedCapability
from mux.state.lease import Slot
from mux.state.store import ERA_REF, StateStore, adopted_identity

log = structlog.get_logger(__name__)


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
    if admission.daimon_rules_need_driver:
        # Daimon tool rules need an approval pause this backend does not have.
        # Refused before any key, transport or journal is touched.
        raise UnsupportedCapability(("daimon_tool_rules",), PROFILE)
    target = request.target_spec
    # Shared ownership, mounts and transfer require a separate complete deployment
    # adapter. Never silently drop those obligations in this slice. Seals ride the
    # turn credential (`template_mcp`).
    if (
        config.thread_mode != "per_caller"
        or admission.shared_owner is not None
        or (
            admission.memory_read_only and (target is None or "memory" not in target.declared_drops)
        )
        or (admission.channel_skills and (target is None or "skills" not in target.declared_drops))
    ):
        raise UnsupportedCapability(("gemini_host_deployment_policy",), PROFILE)
    if not isinstance(runtime.journal, GeminiJournal) or not isinstance(
        runtime.usage_store, GeminiUsageRuntime
    ):
        raise UnsupportedCapability(("gemini_durable_runtime",), PROFILE)
    journal, usage = runtime.journal, runtime.usage_store
    # Gemini usage reaches no ledger debit, and a root interaction has no token
    # envelope: the daily model cap refuses plainly here, before anything is
    # created. A fallback of the first request is checked too (`CappedFlashAccounting`).
    from daimon.core.turn.daily_model_cap import check_daily_model_cap

    await check_daily_model_cap(
        request,
        provider=config.backend,
        model=config.model,
        price=usage.prices.get(config.model) if config.model is not None else None,
        cap=request.deps.gemini_daily_model_cap_usd,
    )
    deployment = journal.deployment
    if admission.backend_revision_origin == "agent":
        from daimon.core.turn.gemini_deployment import load_gemini_deployment
        from daimon.core.turn.runtime_manifest import root_counterpart_catalog_id

        if request.counterpart is None or request.source is None or target is None:
            raise AdmissionDenied(reason="backend_unsupported")
        plan = load_gemini_deployment(request)
        expected = plan.deployment(request)
        checked, ref = request.counterpart, request.counterpart.agent.ref
        if (
            deployment is None
            or replace(deployment, native_agent=None) != expected
            or checked.agent.spec != expected.agent
            or (
                plan.root_materialization
                and checked.catalog_id != root_counterpart_catalog_id(request)
            )
            or (
                not plan.root_materialization
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
        if request.verified_counterpart != checked:
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
    derived_id = str(uuid5(NAMESPACE_URL, f"gemini.host:{slot.model_dump_json()}"))
    # A released slot keeps its id; its era suffix keeps earlier creations from
    # replaying. Only a binding this host adopted (it carries its era) may differ
    # from the derived id.
    binding_id, era = (
        (prior.id if ERA_REF in prior.native_refs else derived_id, "")
        if prior is not None
        else adopted_identity(await store.get_binding(slot, include_released=True), derived_id)
    )
    if prior is not None and (prior.id != binding_id or "session" not in prior.native_refs):
        raise ScopeViolation(binding_id, "Gemini binding differs from admitted revision")
    backend = gemini_backend(config, request.scope, runtime, deployment.account_scope_id)

    def ref_of(bound: ProviderBinding) -> ResourceRef:
        return ResourceRef(
            id=bound.native_refs["session"],
            kind="session",
            provider="gemini",
            account_scope_id=deployment.account_scope_id,
            tenant_id=request.scope.tenant_id,
            account_id=request.scope.account_id,
        )

    async def created(era: str) -> ProviderBinding:
        agent = deployment.native_agent
        if agent is None:
            # The inline agent is created once per session, with this platform's variant of
            # the system prompt (unless the platform prompt is off); a named catalog agent
            # above is shared and keeps its own.
            spec = deployment.agent
            variant = admission.identity.system(spec.system, request.platform)
            if variant is not None:
                spec = spec.model_copy(update={"system": variant})
            agent = await backend.agents.create(request.scope, spec, key=f"{binding_id}{era}:agent")
        environment = (
            await backend.environments.create(
                request.scope, deployment.environment, key=f"{binding_id}{era}:environment"
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
            key=f"{binding_id}{era}:session",
        )
        return session.binding.model_copy(
            update={
                "legacy_account_id": request.scope.account_id,
                "native_refs": {**session.binding.native_refs, **({ERA_REF: era} if era else {})},
            }
        )

    decision = (
        None
        if prior is None
        else binding_decision(
            recorded_facts(prior),
            # Gemini records no model, plan or scope ref: those follow config_revision.
            BindingFacts(
                profile=PROFILE,
                provider="gemini",
                config_revision=config.local,
                agent=(
                    deployment.native_agent.ref.id
                    if deployment.native_agent is not None
                    else prior.native_refs.get("agent", "")
                ),
                account_scope="",
                model="",
                plan_digest="",
            ),
            reuse_existing=request.reuse_existing and request.transfer is None,
        )
    )
    continuity = CONTINUED
    if prior is None:
        carried = await carry_on_adoption(
            request,
            store=store,
            slot=slot,
            model_id=config.model or "",
            agent_name=admission.agent.name,
        )
        if carried is not None:
            continuity = carried.continuity
        binding = await created(era)
        try:
            # A released slot is adopted at its next generation: keep what was written.
            binding = await store.put_binding(binding, expected_generation=0)
        except BindingConflict:
            winner = await store.get_binding(slot)
            if (
                winner is None
                or winner.model_copy(update={"generation": binding.generation}) != binding
            ):
                raise
            binding = winner
        session_ref = ref_of(binding)
    elif isinstance(decision, ReplaceSession):
        replacement = await replace_binding(
            store=store,
            slot=slot,
            scope=request.scope,
            old=OldSession(
                binding=prior,
                ref=ref_of(prior),
                backend=backend if prior.provider == "gemini" else None,
            ),
            reasons=decision.reasons,
            create=lambda era, _mounts, _lease: created(era),
            now=datetime.now(UTC),
            model_id=config.model or "",
            agent_name=admission.agent.name,
        )
        binding, continuity = replacement.binding, replacement.continuity
        session_ref = ref_of(binding)
    else:
        binding = prior
        session = await backend.sessions.retrieve(request.scope, ref_of(prior))
        if (
            session.binding.id != prior.id
            or session.binding.thread != prior.thread
            or session.binding.config_revision != prior.config_revision
        ):
            raise ScopeViolation(
                prior.native_refs["session"], "Gemini session differs from persisted binding"
            )
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
        if request.admission.shown("native_footer_cost", request.platform)
        else None
    )

    async def record(*, observation: UsageObservation) -> bool:
        if observation.session != session_ref or observation.model is None:
            raise ScopeViolation(observation.id, "Gemini billing received foreign usage")
        _warn_on_grounding(observation, tenant_id=request.tenant_id, binding_id=binding_id)
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
            agent_id=current.agent.id,
        )

        if footer_cost is not None:
            footer_cost.note(
                observation,
                price=price,
                infrastructure_usd=usage.infrastructure_usd,
                settled=settled,
            )
        await _settle_hold(usage.accounting, observation, price)
        return settled

    return PreparedTurn(
        admission=replace(current, backend_revision=config),
        ma_session_id=session_ref.id,
        mapping_id=None,
        watermark=None,
        reused=prior is not None and binding is prior,
        session_account_id=request.session_account_id,
        _record=record,
        backend=backend,
        session_ref=session_ref,
        native_footer_cost=footer_cost,
        runtime=runtime if admission.backend_revision_origin == "agent" else None,
        # Revokes a per-turn MCP credential however the turn ends.
        _finalize=runtime.end_of_turn,
        continuity=continuity,
    )


def _warn_on_grounding(
    observation: UsageObservation, *, tenant_id: object, binding_id: str
) -> None:
    """Grounding (search) calls carry a per-query price no tariff or daily cap counts.

    Agent requests never offer them, so any count here is a leak to look at. The
    usage reports them as a list of `{type, count}` (google-genai `GroundingToolCount`).
    """
    raw = observation.native_meter.get("grounding_tool_count")
    entries = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    counts = {
        str(item.get("type")): count
        for item in entries
        if isinstance(count := item.get("count"), int) and not isinstance(count, bool)
    }
    total = sum(counts.values())
    if total > 0:
        structlog.get_logger(__name__).warning(
            "turn.gemini_grounding_unexpected",
            tenant_id=str(tenant_id),
            binding_id=binding_id,
            observation_id=observation.id,
            grounding_tool_count=total,
            grounding_types=sorted(counts),
        )


async def _settle_hold(
    accounting: object, observation: UsageObservation, price: ProviderPrice | None
) -> None:
    """Release the attempt's cap hold once its interaction has ended with a measured cost.

    Billing above already recorded the usage; this only frees cap headroom. A running
    snapshot, a partial meter or an unpriced alias leaves the hold for HOLD_TTL.
    """
    interaction = interaction_of(observation)
    if (
        not isinstance(accounting, HoldSettlement)
        or interaction is None
        or price is None
        # A running snapshot may still grow: only the terminal reading frees the hold.
        or not observation.final
    ):
        return
    assert observation.model is not None
    actual = provider_cost_of(observation, price, infrastructure_usd=Decimal(0))
    if actual is None:
        return
    try:
        await accounting.settle(
            interaction_id=interaction, model=observation.model.id, actual_usd=actual
        )
    except Exception:  # expiry is the fallback; never fail a billed turn
        log.warning("provider_hold_settle_failed", interaction_id=interaction, exc_info=True)


def old_gemini_session(
    runtime: TurnRuntime,
    revision: ConfigRevision,
    scope: Scope,
    binding: ProviderBinding,
    store: StateStore,
) -> OldSession:
    """A released Gemini session, read after its thread moved to another provider."""
    deployment = runtime.journal.deployment if isinstance(runtime.journal, GeminiJournal) else None
    if deployment is None:
        raise UnsupportedCapability(("gemini_pinned_deployment",), PROFILE)
    ref = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider="gemini",
        account_scope_id=deployment.account_scope_id,
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    backend = gemini_backend(revision, scope, runtime, deployment.account_scope_id)
    return OldSession(binding=binding, ref=ref, backend=backend)
