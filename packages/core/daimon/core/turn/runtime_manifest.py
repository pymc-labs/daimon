"""Credential-reference validation without credential discovery or SDK imports."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self, cast, get_type_hints, overload
from uuid import UUID

from daimon.core.errors import DaimonError, TurnError
from daimon.core.neutral_resources import (
    AgentFleetGrant,
    WorkerPermissions,
    WorkerSpecRefused,
    resolve_worker_spec,
)
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.admission import Admission
from daimon.core.turn.ceiling import remaining_s, turn_deadline
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.config import CapabilityRequirement, ConfigRevision, ThreadMode, resolve_default
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, ResourceRef, Scope, SkillRef
from mux.contracts.profile import Capability
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ProviderBinding, ResourceBinding
from mux.errors import MuxError, UnsupportedCapability
from mux.profiles import backend_selection, get_profile, native_agent_tools
from mux.state.operations import OperationRecord, request_digest
from mux.state.store import binding_slot
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

if TYPE_CHECKING:
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.neutral_resources import NeutralResourceHost, ResolvedWorkerSpec
    from daimon.core.stores.domain import BackendAgent, SourceRepresentation, TaskContinuationRow
    from daimon.core.stores.mux_state import BeforeClaim
    from daimon.core.stores.neutral_catalog import AgentRevision
    from daimon.core.turn.deps import TurnDeps
    from daimon.core.turn.persistence import TurnPersistence
    from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
    from mux.contracts.ports import ManagedAgents
    from mux.state.lease import Lease
    from sqlalchemy.ext.asyncio import AsyncSession

_RESERVED = ("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_")
_ROWS = TypeAdapter(list[dict[str, JsonValue]])
_MAX_BYTES = 1_048_576
_ADMISSION_FACTS = {
    name: TypeAdapter(annotation)
    for name, annotation in get_type_hints(Admission).items()
    if name
    not in ("agent", "environment", "observation", "backend_counterpart", "backend_agent_revision")
}


async def read_agent_fleet_grant(
    deps: TurnDeps, admission: Admission, *, now: datetime
) -> AgentFleetGrant | None:
    """Read the current grant for the actual admitted source, without name lookup."""
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.ma_identity import derive_agent_uuid

    if not deps.fleet_enabled:
        return None
    if admission.grant is None:
        raise WorkerSpecRefused("origin", "unbound")
    return await AgentBackendService(deps.sessionmaker, enabled=True).read_fleet_grant(
        admission.grant.tenant_id,
        derive_agent_uuid(tenant_id=admission.grant.tenant_id, ma_agent_id=admission.agent.id),
        now=now,
    )


class _FrozenRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: UUID
    origin_id: UUID
    binding: ProviderBinding
    permissions: WorkerPermissions
    grant_epoch: int | None = Field(default=None, exclude_if=lambda value: value is None)
    grant_digest: str | None = Field(default=None, exclude_if=lambda value: value is None)
    default_backend_revision: ConfigRevision | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    root_controls: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    root_finance: JsonValue = Field(default=None, exclude=True)
    preparation: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class _ResumedRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: UUID
    origin_id: UUID
    root_origin_id: UUID
    run_id: UUID
    wake_id: UUID
    wake_key: UUID
    wake_owner: str
    wake_attempt: int
    wake_started_at: datetime
    scope: Scope
    binding: ProviderBinding


async def _root_context(
    deps: TurnDeps,
    root: _FrozenRoot | None,
    *,
    now: datetime,
    intent_id: UUID | None = None,
    drain: bool = False,
    resumed: _ResumedRoot | None = None,
    execution_origin: bool = False,
) -> TenantAccessPolicy:
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.stores import turn_card_intents, turn_origins
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.stores.accounts import get_account_with_tenant
    from mux.state.operations import request_digest

    parent = root.permissions if root is not None else None
    facts = parent.fleet if parent is not None else None
    if root is None:
        from daimon.core.turn.persistence import current_persistence
        from daimon.core.turn_origin import current_origin_id

        active = current_persistence.get()
        origin_id = current_origin_id.get()
        if (
            active is None
            or origin_id is None
            or intent_id is None
            or not active.source_agent_id
            or active.session_ref is None
            or active.fleet_capture
        ):
            raise WorkerSpecRefused("origin", "unbound")
        binding, scope, source_id = active.binding, active.scope, active.source_agent_id
        active.check_session(scope, active.session_ref)
    else:
        assert parent is not None and facts is not None
        binding, scope = root.binding, facts.scope
        origin_id, intent_id = root.origin_id, root.intent_id
        if (
            parent.agent_id is None
            or len(parent.source_agents) != 1
            or root.grant_epoch is None
            or root.grant_epoch <= 0
            or not root.grant_digest
        ):
            raise WorkerSpecRefused("origin", "stale")
        source_id = parent.source_agents[0].id
    async with deps.sessionmaker() as db:
        origin = await turn_origins.get_active_origin(
            db,
            origin_id=origin_id,
            tenant_id=UUID(scope.tenant_id),
            account_id=UUID(scope.account_id),
            platform=binding.thread.channel.platform,
            now=now,
        )
        intent = await turn_card_intents.get_turn_card_intent(db, id=intent_id)
        account = await get_account_with_tenant(db, account_id=UUID(scope.account_id))
        policy = await load_access_policy(db, tenant_id=UUID(scope.tenant_id))
        if resumed is not None:
            assert root is not None and parent is not None and facts is not None
            execution_intent = await turn_card_intents.get_turn_card_intent(
                db, id=resumed.intent_id
            )
            execution = (
                await turn_origins.get_active_origin(
                    db,
                    origin_id=resumed.origin_id,
                    tenant_id=UUID(scope.tenant_id),
                    account_id=UUID(scope.account_id),
                    platform=binding.thread.channel.platform,
                    now=now,
                )
                if execution_origin
                else None
            )
            if (
                resumed.root_origin_id != root.origin_id
                or resumed.run_id != facts.run_id
                or resumed.binding != binding
                or len(parent.source_agents) != 1
                or (resumed.scope.tenant_id, resumed.scope.account_id, resumed.scope.principal_id)
                != (scope.tenant_id, scope.account_id, scope.principal_id)
                or execution_intent is None
                or (
                    execution_intent.tenant_id,
                    execution_intent.platform,
                    execution_intent.channel_id,
                    execution_intent.thread_id,
                )
                != (
                    UUID(scope.tenant_id),
                    binding.thread.channel.platform,
                    facts.root.channel.channel_id,
                    facts.root.thread_id,
                )
                or execution_intent.status not in ("prepared", "posted")
                or (
                    execution_origin
                    and (
                        execution is None
                        or execution.is_external
                        or execution.is_setup
                        or execution.responder_ma_agent_id != parent.source_agents[0].id
                        or execution.parent_channel_id != facts.root.channel.channel_id
                        or execution.thread_id != facts.root.thread_id
                    )
                )
            ):
                raise WorkerSpecRefused("origin", "stale")
    if root is not None and root.preparation is not None:
        admission_facts = root.preparation.get("admission")
        if isinstance(admission_facts, dict) and admission_facts.get("backend_revision") is None:
            default = ConfigRevision.create(binding.thread.channel, 0, resolve_default(None))
            if (
                admission_facts.get("backend_revision_origin") is not None
                or root.default_backend_revision != default
                or binding.config_revision != default.local
                or binding.provider != default.backend
                or binding.profile != default.profile
            ):
                raise WorkerSpecRefused("origin", "stale")
        elif root.default_backend_revision is not None:
            raise WorkerSpecRefused("origin", "stale")
    if not (origin and intent and account and deps.state_store):
        raise WorkerSpecRefused("origin", "stale")
    if (
        binding.legacy_account_id != scope.account_id
        or origin.responder_ma_agent_id != source_id
        or origin.parent_channel_id != binding.thread.channel.channel_id
        or origin.thread_id != binding.thread.thread_id
        or origin.is_external
        or (account.tenant_id, account.platform) != (origin.tenant_id, origin.platform)
        or account.is_external
        or intent.tenant_id != origin.tenant_id
        or intent.platform != origin.platform
        or intent.channel_id != origin.parent_channel_id
        or intent.thread_id != origin.thread_id
        or intent.status not in ("prepared", "posted")
        or await deps.state_store.get_binding(binding_slot(binding)) != binding
    ):
        raise WorkerSpecRefused("origin", "stale")
    if parent is not None:
        assert facts is not None and root is not None
        if (
            facts.depth != 0
            or not facts.enabled
            or not facts.active
            or facts.origin_id != root.origin_id
            or str(facts.account_id) != scope.account_id
            or facts.root != binding.thread
            or account.platform_user_id != parent.subject.platform_user_id
            or (not drain and parent.limits.deadline <= now)
        ):
            raise WorkerSpecRefused("origin", "stale")
    if parent is None or not drain:
        from daimon.core.channel_backend import AgentBackendRefused
        from daimon.core.ma_identity import derive_agent_uuid

        agent_id = (
            parent.agent_id
            if parent is not None
            else derive_agent_uuid(tenant_id=UUID(scope.tenant_id), ma_agent_id=source_id)
        )
        assert agent_id is not None
        try:
            value = await AgentBackendService(deps.sessionmaker, enabled=True).read_fleet_grant(
                UUID(scope.tenant_id), agent_id, now=now
            )
        except AgentBackendRefused:
            raise WorkerSpecRefused("origin", "unsupported") from None
        if parent is None:
            if value is not None or origin.is_setup:
                raise WorkerSpecRefused("origin", "unbound")
        else:
            assert root is not None
            if (
                value is None
                or value.epoch != root.grant_epoch
                or request_digest(value.model_dump(mode="json")) != root.grant_digest
            ):
                raise WorkerSpecRefused("origin", "stale")
    return policy


async def prepare_controlled_root(
    request: ProviderPreparationRequest, intent_id: UUID | None
) -> ProviderPreparationRequest:
    """Select and reserve finite grant controls before the native compiler runs."""
    from daimon.core.channel_budget import require_root_funding
    from daimon.core.config import load_turn_settings
    from daimon.core.stores import mux_state
    from daimon.core.stores.turn_origins import get_active_origin
    from daimon.core.turn.prepare import _admitted_profile  # pyright: ignore[reportPrivateUsage]
    from daimon.core.turn_origin import current_origin_id
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.state.lease import Slot
    from mux.state.operations import request_digest

    value = (
        await read_agent_fleet_grant(request.deps, request.admission, now=request.now())
        if request.worker is None and request.deps.fleet_enabled
        else None
    )
    if value is None or value.native_root_budget_usd is None:
        return request
    revision, grant = request.admission.backend_revision, request.admission.grant
    # Reuse `_admitted_profile`'s own fallback rather than re-stating the
    # default-backend literal here -- a second site would grow the provider
    # literal count outside the profile/driver seam (`test_provider_literal_ratchet`).
    profile = _admitted_profile(request.admission)
    model = (revision.model if revision else None) or request.admission.agent.model.id
    origin_id = current_origin_id.get()
    if (
        (request.deps.turn_path or load_turn_settings().path) != "mux"
        or grant is None
        or request.admission.shared_owner is not None
        or request.session_account_id != request.admission.account_id
        or get_profile(profile).support.get("session_spend_control") != "native"
        or not any(
            backend.profile == profile and selected.id == model
            for backend, selected in value.choices
        )
    ):
        raise WorkerSpecRefused("native_budget_usd", "unsupported")
    slot = Slot(
        thread=ThreadRef(
            channel=ChannelRef(
                tenant_id=request.scope.tenant_id,
                platform=request.platform,
                channel_id=grant.channel_id,
            ),
            thread_id=request.thread_id,
        ),
        account_id=request.scope.account_id,
    )
    store = request.deps.state_store or mux_state.PostgresStateStore(request.deps.sessionmaker)
    if not isinstance(store, mux_state.PostgresStateStore):
        raise WorkerSpecRefused("native_budget_usd", "unsupported")
    binding = await store.get_binding(slot)
    if binding is not None and intent_id is None:
        return replace(request, native_budget_usd=value.native_root_budget_usd)
    if binding is not None or origin_id is None or intent_id is None:
        raise WorkerSpecRefused("origin", "unbound")
    deadline = min(request.deadline, value.expires_at)
    remaining_s(deadline, now=request.now(), refuse_expired=True)
    controls: dict[str, JsonValue] = {
        "origin_id": str(origin_id),
        "grant_digest": request_digest(value.model_dump(mode="json")),
        "deadline": deadline.isoformat(),
        "max_inputs": min(3, value.max_root_turns),
        "native_budget_usd": str(value.native_root_budget_usd),
        "cash_budget_usd": str(value.root_cash_budget_usd),
        "profile": profile,
        "model": model,
    }
    async with request.deps.sessionmaker.begin() as db:
        await require_root_funding(
            db,
            request.scope,
            slot,
            origin_id=origin_id,
            intent_id=intent_id,
            controls=controls,
            markup=request.deps.markup,
        )
        origin = await get_active_origin(
            db,
            origin_id=origin_id,
            tenant_id=request.tenant_id,
            account_id=request.admission.account_id,
            platform=request.platform,
            now=await mux_state.database_clock(db),
            for_update=True,
        )
        if (
            origin is None
            or origin.is_setup
            or origin.is_external
            or (origin.responder_ma_agent_id, origin.parent_channel_id, origin.thread_id)
            != (request.admission.agent.id, grant.channel_id, request.thread_id)
        ):
            raise WorkerSpecRefused("origin", "unbound")

    caller_guard = request.before_native

    async def guard() -> None:
        if caller_guard is not None:
            await caller_guard()
        remaining_s(deadline, now=request.now(), refuse_expired=True)
        current = await read_agent_fleet_grant(request.deps, request.admission, now=request.now())
        if (
            current is None
            or request_digest(current.model_dump(mode="json")) != controls["grant_digest"]
        ):
            raise WorkerSpecRefused("origin", "stale")

    return replace(
        request,
        deps=replace(request.deps, state_store=store),
        deadline=deadline,
        root_controls=controls,
        native_budget_usd=value.native_root_budget_usd,
        before_native=guard,
    )


async def run_controlled_preparation(
    request: ProviderPreparationRequest, preparing: Awaitable[PreparedTurn]
) -> PreparedTurn:
    from daimon.core.turn.persistence import TurnPersistence
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.state.lease import Slot

    if request.root_controls is None:
        return await preparing
    assert request.admission.grant is not None and request.deps.state_store is not None
    controls = request.root_controls
    origin = UUID(str(controls["origin_id"]))
    slot = Slot(
        thread=ThreadRef(
            channel=ChannelRef(
                tenant_id=request.scope.tenant_id,
                platform=request.platform,
                channel_id=request.admission.grant.channel_id,
            ),
            thread_id=request.thread_id,
        ),
        account_id=request.scope.account_id,
    )
    persistence = TurnPersistence(
        request.deps.state_store,
        None,
        request.scope,
        slot=slot,
        operation_key=f"fleet_root:{origin}",
        now=request.now,
    )
    assert request.before_native is not None
    persistence.root_claim = partial(
        root_claim_check, request.deps, origin, controls, request.before_native
    )

    async def pump() -> PreparedTurn:
        with persistence.activate():
            return await preparing

    try:
        return await persistence.run(pump)
    finally:
        if isinstance(preparing, Coroutine):
            preparing.close()


def root_claim_check(
    deps: TurnDeps,
    origin_id: UUID,
    controls: dict[str, JsonValue],
    guard: Callable[[], Awaitable[object]],
    target: ResourceRef | None,
    count: bool,
) -> BeforeClaim:
    """Keep the original hold and distinct input association in the send transaction."""
    from daimon.core.channel_budget import require_root_funding
    from daimon.core.stores import mux_state
    from daimon.core.usage_delegation import root_usage_outcome_id

    async def check(db: AsyncSession, scope: Scope, key: str, fence: Lease | None) -> bool:
        if fence is None:
            raise WorkerSpecRefused("origin", "unbound")
        root = await require_root_funding(
            db, scope, fence.slot, origin_id=origin_id, controls=controls, markup=deps.markup
        )
        await guard()
        if datetime.fromisoformat(str(controls["deadline"])) <= await mux_state.database_clock(db):
            raise WorkerSpecRefused("origin", "stale")
        if count:
            finance = root.result["root_finance"]
            if target is None or not isinstance(finance, dict):
                raise WorkerSpecRefused("origin", "unbound")
            prior, outcomes, limit = (
                finance.get("input_keys"),
                finance.get("input_outcomes"),
                controls.get("max_inputs"),
            )
            if (
                not isinstance(prior, list)
                or not all(isinstance(k, str) for k in prior)
                or not isinstance(outcomes, dict)
                or set(prior) != set(outcomes)
                or len(set(prior)) != len(prior)
                or type(limit) is not int
                or not 0 < limit <= 3
            ):
                raise WorkerSpecRefused("max_turns", "unbound")
            fresh = key not in prior
            if fresh and len(prior) >= limit:
                raise WorkerSpecRefused("max_turns", "unsupported")
            previous = UUID(str(outcomes[key])) if not fresh and outcomes[key] is not None else None
            outcome = root_usage_outcome_id(scope, target, previous=previous, fresh=fresh)
            if fresh:
                await mux_state.update_fleet_root_finance(
                    db,
                    scope,
                    origin_id=origin_id,
                    root_finance={
                        **finance,
                        "input_keys": [*prior, key],
                        "input_outcomes": {**outcomes, key: str(outcome) if outcome else None},
                    },
                )
        return True

    return check


async def _reserve_root(
    deps: TurnDeps,
    persistence: TurnPersistence,
    intent_id: UUID,
    origin_id: UUID,
    _key: str,
    digest: str,
) -> OperationRecord:
    from daimon.core.stores import mux_state

    async with deps.sessionmaker.begin() as db:
        original = await mux_state.get_fleet_root_operation(
            db, persistence.scope, origin_id=origin_id
        )
        if original is not None and "root_controls" in original.result:
            return original
        record = await mux_state.get_fleet_root_operation(
            db,
            persistence.scope,
            intent_id=intent_id,
            origin_id=origin_id,
            slot=persistence.slot,
            request_digest=digest,
        )
    if record is None:
        raise WorkerSpecRefused("origin", "unbound")
    return record


async def freeze_prepared_root(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    intent_id: UUID,
    now: Callable[[], datetime],
) -> None:
    """Retain the exact host-owned root under its input's existing mutation fence."""
    from uuid import uuid4

    from daimon.core.authz import FleetFacts
    from daimon.core.mux_compat import source_environment_record
    from daimon.core.neutral_resources import fleet_root_permissions
    from daimon.core.session_compat import MaCapabilities
    from daimon.core.stores import fleet
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.turn.persistence import current_persistence
    from daimon.core.turn_origin import current_origin_id
    from mux.state.operations import request_digest

    value = await read_agent_fleet_grant(deps, prepared.admission, now=now())
    if value is None:
        if prepared._source is not None or prepared._preparation is not None:  # pyright: ignore[reportPrivateUsage]
            raise WorkerSpecRefused("origin", "stale")
        return
    persistence, origin_id = current_persistence.get(), current_origin_id.get()
    grant, source = prepared.admission.grant, prepared._source  # pyright: ignore[reportPrivateUsage]
    if not (persistence and origin_id and grant and source):
        raise WorkerSpecRefused("origin", "unbound")
    scope, binding = persistence.scope, persistence.binding
    if (
        not deps.fleet_enabled
        or prepared.admission.shared_owner is not None
        or binding.native_refs.get("session") != prepared.ma_session_id
        or prepared.admission.account_id != UUID(scope.account_id)
        or source.resource_scope != scope
        or not source.source
        or source.source.ref.id != prepared.admission.agent.id
        or binding.config_revision
        != (prepared.admission.backend_revision.local if prepared.admission.backend_revision else 0)
    ):
        raise WorkerSpecRefused("origin", "unbound")
    request = prepared._preparation  # pyright: ignore[reportPrivateUsage]
    if request is None or prepared.admission.backend_counterpart is not None:
        raise WorkerSpecRefused("origin", "unbound")
    admission = prepared.admission
    if grant.thread_id is None:
        from daimon.core.turn.admission import reauthorize

        # A channel mention is admitted before Discord acknowledges its new thread.
        # Bind that grant only to this same-channel execution, then ask the existing
        # policy owner again before retaining any authority for the thread.
        if (
            grant.is_dm
            or request.reuse_existing
            or grant.channel_id != binding.thread.channel.channel_id
            or grant.run_place.channel_id != grant.channel_id
            or grant.turn_place.channel_id != grant.channel_id
        ):
            raise WorkerSpecRefused("origin", "unbound")
        grant = replace(
            grant,
            thread_id=binding.thread.thread_id,
            turn_place=replace(
                grant.turn_place,
                channel_id=binding.thread.thread_id,
                parent_channel_id=grant.channel_id,
            ),
            run_place=replace(
                grant.run_place,
                channel_id=binding.thread.thread_id,
                parent_channel_id=grant.channel_id,
            ),
        )
        admission = await reauthorize(deps, replace(admission, grant=grant))
        if (
            admission.memory_read_only != prepared.admission.memory_read_only
            or admission.asks_before_publishing != prepared.admission.asks_before_publishing
            or admission.origin_seal_ids != prepared.admission.origin_seal_ids
        ):
            raise WorkerSpecRefused("origin", "stale")
    default_revision = None
    if admission.backend_revision is None:
        if admission.backend_revision_origin is not None:
            raise WorkerSpecRefused("origin", "unbound")
        default_revision = ConfigRevision.create(binding.thread.channel, 0, resolve_default(None))
    async with deps.sessionmaker() as db:
        policy = await load_access_policy(db, tenant_id=grant.tenant_id)
    if deps.fleet_root_grants is not None:
        raise WorkerSpecRefused("origin", "unsupported")
    permissions = fleet_root_permissions(
        source,
        grant,
        value,
        fleet=FleetFacts(
            scope=scope,
            root=binding.thread,
            origin_id=origin_id,
            account_id=prepared.admission.account_id,
            run_id=uuid4(),
            place=grant.run_place,
            enabled=True,
            active=True,
        ),
        policy=policy,
        now=now(),
        seal_ids=prepared.admission.origin_seal_ids,
    )
    permissions = replace(
        permissions,
        limits=permissions.limits.model_copy(
            update={"deadline": min(request.deadline, permissions.limits.deadline)}
        ),
    )
    environment = source_environment_record(prepared.admission.environment, scope)
    root = _FrozenRoot(
        intent_id=intent_id,
        origin_id=origin_id,
        binding=binding,
        permissions=permissions,
        default_backend_revision=default_revision,
        root_controls=request.root_controls,
        grant_epoch=value.epoch,
        grant_digest=request_digest(value.model_dump(mode="json")),
        preparation={
            "admission": {
                name: adapter.dump_python(getattr(admission, name), mode="json")
                for name, adapter in _ADMISSION_FACTS.items()
            },
            "source": source.source.model_dump(mode="json", exclude={"native"}),
            "environment": environment.model_dump(mode="json", exclude={"native"}),
            "external_user_id": request.external_user_id,
            "reuse_existing": request.reuse_existing,
            "capabilities": TypeAdapter(MaCapabilities).dump_python(
                request.capabilities, mode="json"
            ),
        },
    )

    async def validate() -> None:
        if (
            root.root_controls is not None
            and root.root_controls.get("grant_digest") != root.grant_digest
        ):
            raise WorkerSpecRefused("origin", "stale")
        await _root_context(deps, root, now=now())

    async def retain(_key: str) -> _FrozenRoot:
        await validate()
        run = await fleet.create_run(
            deps.sessionmaker,
            tenant_id=grant.tenant_id,
            orchestrator_account_id=prepared.admission.account_id,
            orchestrator_session_id=prepared.ma_session_id,
            orchestrator_binding_id=binding.id,
            platform=binding.thread.channel.platform,
            channel_id=grant.channel_id,
            thread_id=binding.thread.thread_id,
            operation_key=f"fleet_root:{origin_id}",
            total_budget_usd=permissions.limits.budget_usd,
            deadline_at=permissions.limits.deadline,
            max_workers=min(deps.fleet_max_workers, value.max_workers),
            created_by="host",
        )
        await validate()
        return root.model_copy(
            update={
                "permissions": replace(permissions, fleet=replace(permissions.fleet, run_id=run.id))
            }
        )

    await validate()
    if root.root_controls is not None:
        persistence.root_claim = partial(
            root_claim_check, deps, root.origin_id, root.root_controls, validate
        )
    await persistence.mutate(
        prepared.session_ref,
        "root",
        root.model_dump(mode="json", exclude={"permissions"}),
        retain,
        _FrozenRoot,
        lambda _: "processed",
        guard=validate,
        retry_failed=False,
        operation_key=f"fleet_root:{origin_id}",
        begin=partial(_reserve_root, deps, persistence, intent_id, origin_id),
    )


async def resume_prepared_root(
    deps: TurnDeps,
    prepared: PreparedTurn,
    row: TaskContinuationRow,
    *,
    intent_id: UUID,
    now: Callable[[], datetime],
) -> None:
    """Retain only a locator to the completion claim's original frozen authority."""
    from daimon.core.continuity.wakes import FrozenWorkerBatch, require_worker_finished_root
    from daimon.core.stores import mux_state
    from daimon.core.turn.persistence import current_persistence
    from daimon.core.turn_origin import current_origin_id

    persistence, origin_id = current_persistence.get(), current_origin_id.get()
    source, grant = prepared._source, prepared.admission.grant  # pyright: ignore[reportPrivateUsage]
    if not (deps.fleet_enabled and persistence and origin_id and source and grant):
        raise WorkerSpecRefused("origin", "unbound")
    if (
        prepared.admission.shared_owner is not None
        or prepared._scope != persistence.scope  # pyright: ignore[reportPrivateUsage]
        or source.resource_scope != persistence.scope
        or row.tenant_id != UUID(persistence.scope.tenant_id)
        or row.requester_account_id != UUID(persistence.scope.account_id)
        or prepared.session_account_id != prepared.admission.account_id
        or grant.subject.platform_user_id != row.requester_external_user_id
        or row.started_at is None
        or not row.lease_owner
    ):
        raise WorkerSpecRefused("origin", "unbound")

    async def claim() -> None:
        await require_worker_finished_root(
            deps.sessionmaker,
            row,
            session_id=prepared.ma_session_id,
            account_id=prepared.admission.account_id,
            agent_id=prepared.admission.agent.id,
            now=now(),
        )

    await claim()
    batch = FrozenWorkerBatch.model_validate_json(row.requested_work or "")
    async with deps.sessionmaker() as db:
        record = await mux_state.get_fleet_root_operation(
            db, persistence.scope, run_id=batch.run_id
        )
    if record is None or record.operation.status != "processed":
        raise WorkerSpecRefused("origin", "unbound")
    try:
        root = _FrozenRoot.model_validate_json(json.dumps(dict(record.result)))
    except ValidationError:
        raise WorkerSpecRefused("origin", "unbound") from None
    locator = _ResumedRoot(
        intent_id=intent_id,
        origin_id=origin_id,
        root_origin_id=root.origin_id,
        run_id=batch.run_id,
        wake_id=row.id,
        wake_key=row.idempotency_key,
        wake_owner=row.lease_owner,
        wake_attempt=row.attempts,
        wake_started_at=row.started_at,
        scope=persistence.scope,
        binding=persistence.binding,
    )

    async def validate() -> None:
        await claim()
        await read_frozen_root(
            deps,
            persistence.scope,
            origin_id=root.origin_id,
            run_id=batch.run_id,
            now=now(),
        )
        await _root_context(deps, root, now=now(), resumed=locator, execution_origin=True)

    async def retain(_key: str) -> _ResumedRoot:
        await validate()
        return locator

    await validate()
    if root.root_controls is not None:
        persistence.root_claim = partial(
            root_claim_check,
            deps,
            root.origin_id,
            root.root_controls,
            lambda: _root_context(deps, root, now=now(), resumed=locator, execution_origin=True),
        )
    await persistence.mutate(
        prepared.session_ref,
        "root",
        locator.model_dump(mode="json"),
        retain,
        _ResumedRoot,
        lambda _: "processed",
        guard=validate,
        retry_failed=False,
        operation_key=f"fleet_root:{origin_id}",
        begin=partial(_reserve_root, deps, persistence, intent_id, origin_id),
    )
    await validate()


async def _read_frozen_root(
    deps: TurnDeps,
    scope: Scope,
    *,
    intent_id: UUID | None = None,
    origin_id: UUID | None = None,
    run_id: UUID | None = None,
    now: datetime,
    drain: bool = False,
) -> tuple[_FrozenRoot, TenantAccessPolicy]:
    """Load one exact retained root, with its original finite authority and current policy."""
    from daimon.core.stores import fleet, mux_state

    if not deps.fleet_enabled or deps.state_store is None:
        raise WorkerSpecRefused("origin", "unbound")
    async with deps.sessionmaker() as db:
        record = await mux_state.get_fleet_root_operation(
            db,
            scope,
            intent_id=intent_id,
            origin_id=origin_id,
        )
    if record is None or record.operation.status != "processed":
        raise WorkerSpecRefused("origin", "unbound")
    resumed = None
    try:
        if "root_origin_id" in record.result:
            resumed = _ResumedRoot.model_validate_json(json.dumps(dict(record.result)))
            if (
                (intent_id is not None and intent_id != resumed.intent_id)
                or (origin_id is not None and origin_id != resumed.origin_id)
                or record.slot != binding_slot(resumed.binding)
            ):
                raise WorkerSpecRefused("origin", "unbound")
            async with deps.sessionmaker() as db:
                record = await mux_state.get_fleet_root_operation(
                    db,
                    scope,
                    origin_id=resumed.root_origin_id,
                )
            if record is None or record.operation.status != "processed":
                raise WorkerSpecRefused("origin", "unbound")
        root = _FrozenRoot.model_validate_json(json.dumps(dict(record.result)))
    except ValidationError:
        raise WorkerSpecRefused("origin", "unbound") from None
    parent, binding = root.permissions, root.binding
    original = parent.fleet.scope
    if (original.tenant_id, original.account_id, original.principal_id) != (
        scope.tenant_id,
        scope.account_id,
        scope.principal_id,
    ) or record.slot != binding_slot(binding):
        raise WorkerSpecRefused("origin", "unbound")
    policy = await _root_context(
        deps,
        root,
        now=now,
        resumed=resumed,
        execution_origin=origin_id is not None,
        drain=drain,
    )
    selected = parent.fleet.run_id
    if run_id is not None and selected != run_id:
        raise WorkerSpecRefused("origin", "unbound")
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=UUID(scope.tenant_id), run_id=selected
    )
    if (
        run.status not in ("running", "collecting")
        or run.orchestrator_session_id != binding.native_refs.get("session")
        or run.orchestrator_binding_id != binding.id
        or run.orchestrator_account_id != parent.fleet.account_id
        or run.platform != binding.thread.channel.platform
        or run.channel_id != binding.thread.channel.channel_id
        or run.thread_id != binding.thread.thread_id
        or run.total_budget_usd != parent.limits.budget_usd
        or run.deadline_at != parent.limits.deadline
    ):
        raise WorkerSpecRefused("origin", "stale")
    return root, policy


@overload
async def read_frozen_root(
    deps: TurnDeps,
    scope: Scope,
    *,
    intent_id: UUID | None = None,
    origin_id: UUID | None = None,
    run_id: UUID | None = None,
    now: datetime,
    drain: bool = False,
    allow_absent: Literal[False] = False,
) -> tuple[WorkerPermissions, TenantAccessPolicy]: ...


@overload
async def read_frozen_root(
    deps: TurnDeps,
    scope: Scope,
    *,
    intent_id: UUID | None = None,
    origin_id: UUID | None = None,
    run_id: UUID | None = None,
    now: datetime,
    drain: bool = False,
    allow_absent: Literal[True],
) -> tuple[WorkerPermissions, TenantAccessPolicy] | None: ...


async def read_frozen_root(
    deps: TurnDeps,
    scope: Scope,
    *,
    intent_id: UUID | None = None,
    origin_id: UUID | None = None,
    run_id: UUID | None = None,
    now: datetime,
    drain: bool = False,
    allow_absent: bool = False,
) -> tuple[WorkerPermissions, TenantAccessPolicy] | None:
    if allow_absent and intent_id is not None and origin_id is None and run_id is None:
        from daimon.core.stores import mux_state
        from daimon.core.turn.persistence import current_persistence

        active = current_persistence.get()
        if active is not None and (
            active.scope.tenant_id,
            active.scope.account_id,
            active.scope.principal_id,
        ) == (scope.tenant_id, scope.account_id, scope.principal_id):
            async with deps.sessionmaker() as db:
                record = await mux_state.get_fleet_root_operation(
                    db, scope, intent_id=intent_id, check_intent_owner=True
                )
            if record is None:
                await _root_context(deps, None, intent_id=intent_id, now=now)
                return None
    root, policy = await _read_frozen_root(
        deps, scope, intent_id=intent_id, origin_id=origin_id, run_id=run_id, now=now, drain=drain
    )
    return root.permissions, policy


async def read_frozen_preparation(
    deps: TurnDeps,
    parent: WorkerPermissions,
    *,
    deadline: datetime,
    now: Callable[[], datetime],
) -> ProviderPreparationRequest:
    """Reconstruct the exact retained root request under the original finite authority."""
    from daimon.core.mux_compat import (
        retrieve_environment,
        sdk_agent,
        sdk_environment,
        source_environment_record,
    )
    from daimon.core.session_compat import MaCapabilities
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from mux.contracts.resources import Agent, Environment
    from mux.state.operations import request_digest

    root, _ = await _read_frozen_root(
        deps,
        parent.fleet.scope,
        origin_id=parent.fleet.origin_id,
        run_id=parent.fleet.run_id,
        now=now(),
    )
    if root.permissions != parent or root.preparation is None:
        raise WorkerSpecRefused("origin", "unbound")
    bound = min(deadline, parent.limits.deadline)
    async with asyncio.timeout(remaining_s(bound, now=now(), refuse_expired=True)):
        try:
            facts = root.preparation
            admission_facts = facts["admission"]
            if not isinstance(admission_facts, dict) or set(admission_facts) != set(
                _ADMISSION_FACTS
            ):
                raise WorkerSpecRefused("origin", "unbound")
            retained_source = Agent.model_validate_json(json.dumps(facts["source"]))
            retained_env = Environment.model_validate_json(json.dumps(facts["environment"]))
            if (
                (retained_source.ref, retained_source.revision) not in parent.source_revisions
                or (retained_env.ref.tenant_id, retained_env.ref.account_id)
                != (parent.fleet.scope.tenant_id, parent.fleet.scope.account_id)
                or retained_source.native is not None
                or retained_env.native is not None
            ):
                raise WorkerSpecRefused("scope", "unbound")
            admission = Admission(
                agent=sdk_agent(retained_source),
                environment=sdk_environment(retained_env),
                **{
                    name: adapter.validate_json(json.dumps(admission_facts[name]))
                    for name, adapter in _ADMISSION_FACTS.items()
                },
            )
            request = ProviderPreparationRequest(
                deps,
                admission,
                parent.fleet.scope,
                UUID(parent.fleet.scope.tenant_id),
                root.binding.thread.channel.platform,
                TypeAdapter(str).validate_python(facts["external_user_id"]),
                root.binding.thread.thread_id,
                parent.fleet.account_id,
                TypeAdapter(bool).validate_python(facts["reuse_existing"]),
                TypeAdapter(MaCapabilities).validate_python(facts["capabilities"]),
                None,
                bound,
                now,
            )
        except (ValidationError, KeyError):
            raise WorkerSpecRefused("origin", "unbound") from None
        if admission.backend_revision_origin == "agent" and admission.backend_revision is not None:
            from daimon.core.ma_identity import derive_agent_uuid
            from daimon.core.stores import mux_state

            # Reuse the original immutable consent association, never the latest preference.
            async with deps.sessionmaker() as db:
                association = await mux_state.get_config_agent_revision(
                    db,
                    admission.backend_revision,
                    agent_id=derive_agent_uuid(
                        tenant_id=UUID(parent.fleet.scope.tenant_id),
                        ma_agent_id=retained_source.ref.id,
                    ),
                )
            if association is None:
                raise WorkerSpecRefused("origin", "unbound")
            request = replace(
                request,
                admission=replace(admission, backend_agent_revision=association),
            )
        request = await request.capture_root(parent, deadline=bound)
        if (
            request.source is None
            or request.source.source is None
            or request.source.source.spec != retained_source.spec
        ):
            raise WorkerSpecRefused("scope", "stale")
        await _root_context(deps, root, now=now())
        actual_env = await retrieve_environment(
            deps.anthropic, retained_env.ref.id, scope=parent.fleet.scope
        )
        actual = source_environment_record(actual_env, parent.fleet.scope)
        if (
            actual.ref != retained_env.ref
            or actual.revision != retained_env.revision
            or actual.lifecycle != "active"
            or actual.archived_at is not None
            or request_digest(actual.spec.model_dump(mode="json"))
            != request_digest(retained_env.spec.model_dump(mode="json"))
        ):
            raise WorkerSpecRefused("scope", "stale")
        request = replace(request, admission=replace(request.admission, environment=actual_env))
        # Refresh the captured resources from the verified native readback while
        # preserving the source-first refusal order above.
        request = replace(
            request, source=await capture_preparation_source(replace(request, source=None))
        )
        policy = await _root_context(deps, root, now=now())
        resolve_worker_spec(
            parent,
            parent.spec,
            policy=policy,
            now=now(),
            resources=parent.resources,
            environment=parent.environment,
            limits=parent.limits.for_provider(parent.spec.model.provider),
            captured_source=request.source,
        )
        remaining_s(bound, now=now(), refuse_expired=True)
        return request


class ProviderRuntimeConfigurationError(RuntimeError):
    """Static, typed operator errors; never include manifest content or paths."""

    def __init__(self, reason: Literal["reserved_key_reference", "invalid_manifest"]) -> None:
        self.reason = reason
        message = (
            "Provider runtime api_key_env uses a reserved provider prefix "
            "(OPENAI_, ANTHROPIC_, GOOGLE_, GEMINI_). Use a host-scoped name such as "
            "DAIMON_CHANNEL_OPENAI_KEY or DAIMON_CHANNEL_GEMINI_KEY."
            if reason == "reserved_key_reference"
            else "Provider runtime manifest must be a readable JSON list of objects, at most 1 MiB."
        )
        super().__init__(message)


def validate_api_key_env(value: str) -> str:
    """Refuse SDK ambient-variable namespaces before looking up any secret."""
    if value.upper().startswith(_RESERVED):
        raise ProviderRuntimeConfigurationError("reserved_key_reference")
    return value


ApiKeyEnvironment = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
    AfterValidator(validate_api_key_env),
]


def validate_configured_runtime_manifest() -> None:
    """Check an explicitly configured manifest's references, without constructing plans.

    Settings loaders call this only with mux and channel backends enabled. Missing
    manifests retain the existing selected-turn refusal; no discovery happens here.
    Scope/digest, full provider policy and key availability remain admission checks.
    """
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not path:
        return
    try:
        with Path(path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ProviderRuntimeConfigurationError("invalid_manifest")
        rows = _ROWS.validate_json(raw)
    except (OSError, ValidationError):
        raise ProviderRuntimeConfigurationError("invalid_manifest") from None
    for row in rows:
        reference = row.get("api_key_env")
        if isinstance(reference, str):
            validate_api_key_env(reference)


class ConfigurationGrant(BaseModel):
    """Finite operator-authorized resolved fields; no wildcard defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    profiles: tuple[str, ...] = Field(min_length=1)
    models: tuple[str | None, ...] = Field(min_length=1)
    thread_modes: tuple[ThreadMode, ...] = Field(min_length=1)
    requires: dict[Capability, CapabilityRequirement]


class SourceSkillMapping(BaseModel):
    """A concrete source version and an independently deployed full-byte target."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: SkillRef
    target: SkillRef
    archive: Path

    @model_validator(mode="after")
    def _concrete(self) -> Self:
        if (
            not self.archive.is_absolute()
            or self.source.version in (None, "latest")
            or self.target.version in (None, "latest")
            or self.target.digest is None
        ):
            raise ValueError("skill representation requires a concrete version and full archive")
        return self


class SourceCapabilities(BaseModel):
    """Operator-selected capability inputs; source capture must verify every byte."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_pin: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: ModelRef
    skills: tuple[SourceSkillMapping, ...]
    mcp_tools: tuple[str, ...] = Field(min_length=1)
    environment: EnvironmentSpec | None = None

    @model_validator(mode="after")
    def _unique(self) -> Self:
        profile = get_profile(backend_selection(self.model.provider).profile)
        if (
            len({item.source.id for item in self.skills}) != len(self.skills)
            or len({item.target.id for item in self.skills}) != len(self.skills)
            or len(set(self.mcp_tools)) != len(self.mcp_tools)
            or self.model.options
            or (
                profile.skill_digest == "version"
                and any(item.target.digest != item.target.version for item in self.skills)
            )
        ):
            raise ValueError("source capability representation must be complete and unambiguous")
        return self


def configuration_matches(
    revision: ConfigRevision, *, digest: str | None, grant: ConfigurationGrant | None
) -> bool:
    """Legacy exact grants remain exact; bounded grants authorize only declared fields."""
    if grant is None:
        return digest == revision.digest
    return (
        revision.profile in grant.profiles
        and revision.model in grant.models
        and revision.thread_mode in grant.thread_modes
        and dict(revision.requires) == grant.requires
    )


def manifest_configuration_matches(row: Mapping[str, JsonValue], revision: ConfigRevision) -> bool:
    """Match before any credential lookup; invalid grants refuse at the loader boundary."""
    raw = row.get("configuration_grant")
    grant = ConfigurationGrant.model_validate(raw) if raw is not None else None
    digest = row.get("config_digest")
    return configuration_matches(
        revision, digest=digest if isinstance(digest, str) else None, grant=grant
    )


def fixed_plan_origin_allowed(origin: str | None) -> bool:
    """Only channel-origin plans bypass the complete authorized-source proof."""
    return origin == "channel"


async def capture_preparation_source(
    request: ProviderPreparationRequest, *, fresh: bool = False, capability_preview: bool = False
) -> BackendAgent:
    """Capture source facts; the existing proof and materializer validate them."""
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
    from daimon.core.sessions import project_session_resources

    if fresh:
        from daimon.core.mux_compat import retrieve_agent, retrieve_environment

        actual = await retrieve_agent(
            request.deps.anthropic, request.admission.agent.id, scope=request.scope
        )
        environment = await retrieve_environment(
            request.deps.anthropic, request.admission.environment.id, scope=request.scope
        )
        request = replace(
            request,
            source=None,
            admission=replace(request.admission, agent=actual, environment=environment),
        )
    source = request.source
    if source is None:
        source = await load_backend_agent(
            request.deps.anthropic,
            request.deps.sessionmaker,
            tenant_id=request.tenant_id,
            agent_name=(request.admission.agent.metadata or {}).get(
                MA_METADATA_KEY_NAME, request.admission.config.agent_name
            )
            or "",
            actual_agent=request.admission.agent,
        )
    captured = await project_session_resources(
        source,
        agent=request.admission.agent,
        environment=request.admission.environment,
        session_factory=request.deps.sessionmaker,
        tenant_id=request.tenant_id,
        agent_uuid=source.agent_id,
        account_id=(
            None if request.admission.shared_owner is not None else request.admission.account_id
        ),
        resource_scope=request.scope,
        memory_read_only=request.admission.memory_read_only,
        fernet=request.deps.fernet,
        channel_skills=request.admission.channel_skills,
        tool_safety=request.deps.tool_safety,
        asks_before_publishing=request.admission.asks_before_publishing,
        mcp_settings=request.deps.mcp,
        agent_github_app=request.deps.agent_github_app,
        is_external=request.admission.is_external,
        hide_personal_for=request.admission.shared_owner,
        slack_turn_context_id=request.admission.slack_turn_context_id,
        private_dm_id=request.admission.private_dm_id,
        origin_seal_ids=request.admission.origin_seal_ids,
    )
    return await _capture_source_capabilities(
        request, captured, capability_preview=capability_preview
    )


async def _capture_source_capabilities(
    request: ProviderPreparationRequest, source: BackendAgent, *, capability_preview: bool = False
) -> BackendAgent:
    """Verify configured representations at the common preview/preparation capture edge.

    No configured representation means no archive, catalogue or provider reads.
    Native target ownership/readback remains a separate mandatory preparation gate.
    """
    from anthropic import AnthropicError
    from daimon.core.defaults.ma_index import download_skill_version
    from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti, validate_agent_mcp_token
    from daimon.core.mcp_oauth.discovery import list_mcp_tools
    from daimon.core.mux_backend import managed_agents
    from daimon.core.mux_compat import legacy_call
    from daimon.core.skills.ingest import retained_archive_files
    from daimon.core.stores.domain import SourceRepresentation
    from daimon.core.stores.mcp_tokens import revoke_mcp_token
    from mux.contracts.resources import MCPConnection, SkillUpload, SkillUploadFile
    from mux.drivers.anthropic.schemas import AGENT_TOOLS_NAMESPACE, agent_tools
    from mux.profiles import represented_execution_tools
    from sqlalchemy.exc import SQLAlchemyError

    source = source.model_copy(update={"representations": ()})
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if path is None:
        return source
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("manifest too large")
        rows = _ROWS.validate_json(raw)
    except (OSError, ValueError):
        # This optional capability evidence cannot break an ordinary source
        # capture. A full deployment still refuses without the representation,
        # and its closed runtime-manifest loader independently rejects bad input.
        return source
    revision = request.admission.backend_revision
    if (
        request.worker is not None
        or request.admission.private_dm_id is not None
        or request.admission.slack_turn_context_id is not None
        or request.admission.origin_channel_id is None
        or not capability_preview
        and (revision is None or request.admission.backend_revision_origin != "agent")
    ):
        return source
    try:
        selected = [
            row
            for row in rows
            if row.get("source_capabilities") is not None
            and row.get("platform") == request.platform
            and row.get("channel_id") == request.admission.origin_channel_id
            and (
                capability_preview
                or revision is not None
                and row.get("channel_id") == revision.channel.channel_id
                and row.get("profile") == revision.profile
            )
            and row.get("tenant_id") == str(source.tenant_id)
            and row.get("account_id") == request.scope.account_id
            and row.get("source_agent_id") == str(source.agent_id)
            and source.source is not None
            and row.get("source_agent_version") == source.source.revision.local
        ]
        if not selected:
            return source
        spec, scope = source.effective_spec, source.resource_scope
        if spec is None or scope is None or scope != request.scope:
            raise ValueError("missing captured source")
        pin = source_preservation_pin(source)
        represented: list[SourceRepresentation] = []
        for row in selected:
            inputs = SourceCapabilities.model_validate(row["source_capabilities"])
            profile_id = row.get("profile")
            if not isinstance(profile_id, str):
                raise ValueError("missing representation profile")
            profile = get_profile(profile_id)
            if (
                inputs.source_pin != pin
                or profile.provider != inputs.model.provider
                or profile.skill_digest not in ("version", "content")
                or row.get("platform") != request.platform
                or row.get("channel_id") != request.admission.origin_channel_id
                and row.get("channel_id")
                != (
                    request.admission.backend_revision.channel.channel_id
                    if request.admission.backend_revision
                    else None
                )
            ):
                raise ValueError("representation differs from captured source")
            if inputs.environment is not None and (
                not profile.implicit_execution
                or source.environment is None
                or inputs.environment != source.environment.spec
                or inputs.environment.native_config is not None
                or inputs.environment.execution != "hosted"
                or inputs.environment.network is None
                or inputs.environment.packages is None
            ):
                raise ValueError("environment representation differs from captured policy")
            if request.deps.mcp.public_url is None or request.deps.mcp.jwt_secret is None:
                raise ValueError("MCP catalogue requires the captured caller")
            # Use the runtime's registered chat-execution authority, never the
            # restricted external-agent key (which sees a different catalogue).
            # This preview credential is revoked before returning and never
            # exposed to a provider; the ACK owner mints the runtime credential.
            now = request.now()
            secret = request.deps.mcp.jwt_secret.get_secret_value().encode()
            async with request.deps.sessionmaker.begin() as db:
                catalogue_token = await mint_agent_mcp_token(
                    db,
                    account_id=request.admission.account_id,
                    tenant_id=source.tenant_id,
                    agent_id=source.agent_id,
                    label="source-catalogue",
                    secret=secret,
                    now=now,
                    ttl_days=900 / 86400,  # pyright: ignore[reportArgumentType]
                    chat_execution=True,
                )
            try:
                await validate_agent_mcp_token(
                    request.deps.sessionmaker,
                    token=catalogue_token,
                    secret=secret,
                    scope=scope,
                    agent_id=source.agent_id,
                    now=now,
                    chat_execution=True,
                )
                catalogue = await list_mcp_tools(str(request.deps.mcp.public_url), catalogue_token)
            finally:
                async with request.deps.sessionmaker.begin() as db:
                    await revoke_mcp_token(db, jti=token_jti(catalogue_token), now=request.now())
            allowed = next(
                (
                    server.tool_policy.get("allowed_tools")
                    for server in spec.mcp_servers or ()
                    if server.name == DAIMON_SERVER_NAME
                ),
                None,
            )
            if allowed is not None:
                if not isinstance(allowed, list) or any(
                    not isinstance(name, str) for name in allowed
                ):
                    raise ValueError("invalid source MCP allowlist")
                if frozenset(allowed) != catalogue:
                    raise ValueError("source MCP allowlist differs from complete server catalogue")
                catalogue = frozenset(cast(list[str], allowed))
            if frozenset(inputs.mcp_tools) != catalogue:
                raise ValueError("MCP representation must preserve the exact source catalogue")
            tools = tuple(spec.tools or ())
            for extension in spec.extensions or ():
                if extension.namespace == AGENT_TOOLS_NAMESPACE:
                    tools = agent_tools(extension).intent(
                        exact=True, mcp_catalogues={DAIMON_SERVER_NAME: catalogue}
                    )
            if not any(
                tool.kind == "mcp_toolset"
                and tool.name == DAIMON_SERVER_NAME
                and tool.permission == "auto"
                for tool in tools
            ):
                raise ValueError("source has no enabled daimon-mcp toolset")
            mapped_tools = represented_execution_tools(
                profile, tuple(tool for tool in tools if tool.kind != "mcp_toolset")
            )
            if any(
                tool.kind == "mcp_toolset"
                and (tool.name != DAIMON_SERVER_NAME or tool.permission != "auto")
                for tool in tools
            ):
                raise ValueError("unrepresented MCP toolset")
            reference = f"caller-mcp:{source.tenant_id}:{scope.account_id}:{source.agent_id}"
            servers = tuple(spec.mcp_servers or ())
            if not servers:
                # The composer supplies this caller-bound capability even when
                # it is absent from the persistent source agent definition.
                servers = (
                    MCPConnection(name=DAIMON_SERVER_NAME, url=str(request.deps.mcp.public_url)),
                )
            if (
                len(servers) != 1
                or servers[0].name != DAIMON_SERVER_NAME
                or servers[0].transport != "streamable_http"
                or servers[0].url.rstrip("/") != str(request.deps.mcp.public_url).rstrip("/")
                or not any(
                    binding.credential_ref == reference
                    and _mcp_credential_servers(source, binding) == frozenset({DAIMON_SERVER_NAME})
                    for binding in source.resource_requirements
                )
                or servers[0].credential_ref not in (None, reference)
            ):
                raise ValueError("MCP representation requires its original caller authority")
            if set(servers[0].tool_policy) - {"allowed_tools"}:
                raise ValueError("unrepresented MCP tool policy")
            allowed = servers[0].tool_policy.get("allowed_tools")
            if allowed is not None and (
                not isinstance(allowed, list)
                or len(allowed) != len(inputs.mcp_tools)
                or set(inputs.mcp_tools) != set(allowed)
            ):
                raise ValueError("MCP representation expands the source allowlist")
            connection = servers[0].model_copy(
                update={
                    "credential_ref": reference,
                    "tool_policy": {"allowed_tools": list(inputs.mcp_tools)},
                }
            )
            if {item.source.id for item in inputs.skills} != {s.id for s in spec.skills or ()}:
                raise ValueError("skill representation is incomplete")
            unresolved = tuple(s for s in spec.skills or () if s.version in (None, "latest"))
            latest: dict[str, str] = {}
            if unresolved:
                # Grant reads only for ids captured from this actual source.
                # A scoped catalogue walk deliberately hides custom skills.
                backend = managed_agents(
                    request.deps.anthropic,
                    scope=scope,
                    resources=frozenset(("skill", s.id) for s in unresolved),
                )

                async def resolve_latest(
                    attached: SkillRef, selected: ManagedAgents = backend
                ) -> tuple[str, str]:
                    record = await legacy_call(selected.skills.retrieve(scope, attached.id))
                    pin = record.latest_version
                    if (
                        record.id != attached.id
                        or record.source != attached.source
                        or pin is None
                        or pin.id != attached.id
                        or pin.version in (None, "latest")
                    ):
                        raise ValueError("source skill has no concrete current version")
                    assert pin.version is not None
                    return attached.id, pin.version

                latest = dict(await asyncio.gather(*(resolve_latest(s) for s in unresolved)))

            async def verify_skill(
                item: SourceSkillMapping, versions: Mapping[str, str] = latest
            ) -> None:
                attached = next(s for s in spec.skills or () if s.id == item.source.id)
                version = (
                    versions.get(attached.id)
                    if attached.version in (None, "latest")
                    else attached.version
                )
                if (
                    not isinstance(version, str)
                    or version != item.source.version
                    or item.source.source != attached.source
                ):
                    raise ValueError("source skill version changed")
                original_files = retained_archive_files(
                    await download_skill_version(
                        request.deps.anthropic,
                        skill_id=item.source.id,
                        version=version,
                        scope=scope,
                    )
                )
                retained = retained_archive_files(item.archive.read_bytes())
                if original_files != retained:
                    raise ValueError("represented skill bytes changed")
                canonical = SkillUpload(
                    files=tuple(
                        SkillUploadFile(path=path, content=body)
                        for path, body in sorted(retained.items())
                    )
                )
                if request_digest(canonical.model_dump(mode="json")) != item.target.digest:
                    raise ValueError("target skill digest differs from source bytes")

            await asyncio.gather(*(verify_skill(item) for item in inputs.skills))
            target = AgentSpec(
                name=spec.name,
                description=spec.description,
                model=inputs.model,
                system=spec.system,
                metadata=spec.metadata,
                tools=mapped_tools,
                skills=tuple(item.target for item in inputs.skills),
                mcp_servers=(connection,),
            )
            represented.append(
                SourceRepresentation(
                    source_pin=pin,
                    target=target,
                    credential_refs=(reference,),
                    environment=inputs.environment,
                )
            )
        if len({item.target.model.model_dump_json() for item in represented}) != len(represented):
            raise ValueError("ambiguous source representation")
        return source.model_copy(update={"representations": tuple(represented)})
    except (OSError, ValueError, DaimonError, MuxError, AnthropicError, SQLAlchemyError):
        # Representations are optional capture evidence, never a new failure
        # mode for native callers or worker ceilings. Full deployments require
        # this evidence independently and refuse before any provider exposure.
        return source


def source_instructions_preserved(required: str | None, system: str | None) -> bool:
    """Retained source text is an exact prefix, with a paragraph boundary."""
    return (
        required is None
        or system is not None
        and (system == required or system.startswith(required + "\n\n"))
    )


def compose_source_instructions(
    required: str | None, system: str | None, *, deduplicate_prefix: bool = False
) -> str:
    """Preserve the source prefix once, retaining the target's remaining paragraphs."""
    if source_instructions_preserved(required, system):
        return system or ""
    assert required is not None
    if not deduplicate_prefix:
        return required + ("\n\n" + system if system else "")
    suffix = (system or "").split("\n\n") if system else []
    common = 0
    for left, right in zip(required.split("\n\n"), suffix, strict=False):
        if left != right:
            break
        common += 1
    rest = "\n\n".join(suffix[common:])
    return required + ("\n\n" + rest if rest else "")


def _source_extension_obligation(
    extension: ExtensionConfig,
    *,
    native_target: AgentSpec | None = None,
    foreign_target: bool = False,
) -> Literal["model_controls", "source_definition"] | None:
    if extension.version != 1:
        return "source_definition"
    if extension.namespace == "anthropic.model_config":
        from mux.drivers.anthropic.schemas import AgentModelConfig

        try:
            native = AgentModelConfig.model_validate(dict(extension.value))
        except ValueError:
            return "source_definition"
        if native_target is not None and extension in (native_target.extensions or ()):
            model_id = native.model if isinstance(native.model, str) else native.model.id
            if model_id != native_target.model.id:
                return "source_definition"
            profile = get_profile(backend_selection(native_target.model.provider).profile)
            if any(
                offered.namespace == extension.namespace and offered.version == extension.version
                for offered in profile.extensions
            ):
                # The owning native compiler emits the complete unchanged model object.
                return None
        model = extension.value.get("model")
        if isinstance(model, dict):
            if set(model) <= {"id", "speed"} and model.get("speed") is None:
                return None
            if foreign_target:
                return "model_controls"
        return "source_definition"
    if (
        extension.namespace == "anthropic.multiagent"
        and extension.value == {"clear": True}
        or extension.namespace == "anthropic.agent_create_nulls"
    ):
        return None
    return "source_definition"


def _mcp_credential_servers(source: BackendAgent, binding: ResourceBinding) -> frozenset[str]:
    """Pair only owner-projected credentials with their MCP capability."""
    if (
        binding.kind != "credential"
        or binding.credential_ref is None
        or binding.native is not None
        or binding.resource is not None
        or binding.target_path is not None
    ):
        return frozenset()
    paired: set[str] = (
        {
            server.name
            for server in source.effective_spec.mcp_servers or ()
            if server.credential_ref == binding.credential_ref
        }
        if source.effective_spec is not None
        else set()
    )
    if source.resource_scope is not None and binding.id == binding.credential_ref:
        caller = (
            f"caller-mcp:{source.tenant_id}:{source.resource_scope.account_id}:{source.agent_id}"
        )
        if binding.id == caller:
            paired.add(DAIMON_SERVER_NAME)
        elif binding.id.startswith(caller + ":slack-turn:"):
            try:
                UUID(binding.id.removeprefix(caller + ":slack-turn:"))
            except ValueError:
                pass
            else:
                paired.add(DAIMON_SERVER_NAME)
    return frozenset(paired)


def worker_source_ceiling(source: BackendAgent) -> BackendAgent:
    """Exclude validated native-only capabilities and their paired resources."""
    spec = source.effective_spec
    if spec is None:
        return source
    normalized, excluded = worker_spec_ceiling(spec)
    resources = tuple(
        binding
        for binding in source.resource_requirements
        if not ((paired := _mcp_credential_servers(source, binding)) and paired <= excluded)
    )
    return source.model_copy(
        update={
            "effective_spec": normalized,
            "resource_requirements": resources,
        }
    )


def worker_spec_ceiling(spec: AgentSpec) -> tuple[AgentSpec, frozenset[str]]:
    """Normalize native toolsets without changing unrelated field presence."""
    from mux.drivers.anthropic.schemas import AGENT_TOOLS_NAMESPACE, agent_tools

    updates: dict[str, object] = {}
    excluded: set[str] = set()
    retained: list[ExtensionConfig] = []
    for extension in spec.extensions or ():
        if extension.namespace == AGENT_TOOLS_NAMESPACE:
            native = agent_tools(extension)
            updates["tools"] = native.intent()
            excluded.update(native.excluded_mcp_servers())
        elif _source_extension_obligation(extension) is not None:
            retained.append(extension)
    if len(retained) != len(spec.extensions or ()):
        updates["extensions"] = tuple(retained) or None
    if any(server.name in excluded for server in spec.mcp_servers or ()):
        updates["mcp_servers"] = tuple(
            server for server in spec.mcp_servers or () if server.name not in excluded
        )
    return spec.model_copy(update=updates) if updates else spec, frozenset(excluded)


def worker_model_spec(ceiling: AgentSpec, target: AgentSpec) -> AgentSpec:
    """Carry granted native controls exactly, or require their declared loss."""
    normalized, _ = worker_spec_ceiling(ceiling)
    controls = tuple(normalized.extensions or ())
    if len(controls) > 1 or any(
        _source_extension_obligation(extension, foreign_target=True) != "model_controls"
        for extension in controls
    ):
        raise ValueError("unsupported native worker ceiling")
    if target.extensions and target.extensions != controls:
        raise ValueError("worker native controls differ from the ceiling")
    if not controls:
        return target
    extension = controls[0]
    if target.model.provider == ceiling.model.provider:
        retained = target.model_copy(update={"extensions": controls})
        if _source_extension_obligation(extension, native_target=retained) is not None:
            raise ValueError("native worker model does not represent the granted controls")
        return retained
    if "model_controls" not in target.declared_drops:
        raise ValueError("native model controls require an explicit declared loss")
    return target.model_copy(update={"extensions": None})


def classify_source_obligations(
    source: BackendAgent,
    target: AgentSpec | None,
    *,
    environment: EnvironmentSpec | None,
    resources: tuple[ResourceBinding, ...] = (),
    asks_before_publishing: bool = False,
    worker_ceiling: bool = False,
    retained_agent: ResourceRef | None = None,
) -> frozenset[str]:
    """One source-preservation rule for pre-allocation normalization and preparation."""
    if worker_ceiling:
        try:
            source = worker_source_ceiling(source)
        except ValueError:
            return frozenset({"source_definition", "tools"})
    spec = source.effective_spec
    if spec is None:
        return frozenset({"source_definition"})
    drops = target.declared_drops if target is not None else ()
    represented = represented_source(source, target)
    # Existing native sessions retain their agent and composer inputs;
    # this proof never authorizes a newly compiled worker or counterpart.
    native_retained = (
        not worker_ceiling
        and retained_agent is not None
        and source.source is not None
        and retained_agent == source.source.ref
        and retained_agent.kind == "agent"
        and source.source.native is not None
        and source.resource_scope is not None
        and source.source.ref.tenant_id == source.resource_scope.tenant_id
        and str(source.tenant_id) == source.resource_scope.tenant_id
        and source.source.lifecycle == "active"
        and source.source.archived_at is None
        and target == spec
        and retained_agent.provider == spec.model.provider
        and not drops
        and source.environment is not None
        and source.environment.lifecycle == "active"
        and source.environment.archived_at is None
        and environment == source.environment.spec
        and resources == source.resource_requirements
    )
    missing: set[str] = set()
    if not source_instructions_preserved(spec.system, target.system if target else None):
        missing.add("instructions")
    source_tools = tuple(spec.tools or ())
    from mux.drivers.anthropic.schemas import AGENT_TOOLS_NAMESPACE, agent_tools

    for extension in spec.extensions or ():
        if extension.version != 1:
            missing.add("source_definition")
        elif extension.namespace == AGENT_TOOLS_NAMESPACE:
            try:
                native = agent_tools(extension)
            except ValueError:
                missing.update(("source_definition", "tools"))
            else:
                if target is not None and "tools" not in drops:
                    source_tools = native.intent()
                    if extension in (target.extensions or ()):
                        # The native compiler preserves this exact tool payload,
                        # overriding the target's neutral tools.
                        source_tools = tuple(target.tools or ())
                    elif not represented and native.has_unrepresented_toolset():
                        missing.add("tools")
        else:
            foreign_target = target is not None and target.model.provider != spec.model.provider
            obligation = _source_extension_obligation(
                extension,
                native_target=target if not foreign_target else None,
                foreign_target=foreign_target,
            )
            if obligation is not None and not (
                obligation == "model_controls" and obligation in drops
            ):
                missing.add(obligation)
    profile = get_profile(backend_selection(spec.model.provider).profile)
    tools_required = bool(
        spec.tools or any(item.value.get("tools") for item in spec.extensions or ())
    )
    if asks_before_publishing or (
        tools_required
        and "tools" not in drops
        and (
            target is None
            or not represented
            and (
                native_agent_tools(profile, spec.model_copy(update={"tools": source_tools}))
                != native_agent_tools(profile, target)
            )
        )
    ):
        missing.add("tools")
    if (
        spec.skills
        and "skills" not in drops
        and (target is None or not represented and target.skills != spec.skills)
    ):
        missing.add("skills")
    if (
        spec.mcp_servers
        and "mcp" not in drops
        and (target is None or not represented and target.mcp_servers != spec.mcp_servers)
    ):
        # `mcp` is its own obligation: declaring `tools` alone must never
        # silently discharge it — an excluded mcp toolset requires an
        # explicit `mcp` (and/or `tools`) declaration, never implied.
        missing.add("mcp")
    if source.repository_sources and "repository" not in drops and not native_retained:
        missing.add("repository")
    retained_servers = (target.mcp_servers or ()) if target is not None else ()
    retained_mcp = {server.name for server in retained_servers}
    retained_credentials = {binding.credential_ref for binding in resources} | {
        server.credential_ref for server in retained_servers
    }
    retained_credentials.discard(None)
    for server in spec.mcp_servers or ():
        if server.credential_ref and not native_retained:
            if represented and server.credential_ref in represented.credential_refs:
                continue
            if server.name in retained_mcp or server.credential_ref in retained_credentials:
                missing.add("credentials")
            elif "mcp" not in drops:
                missing.add("mcp")
    if not native_retained and any(
        repository.credential_ref for repository in source.repository_sources
    ):
        missing.add("credentials")
    for resource in source.resource_requirements:
        if (
            native_retained
            and resource.kind == "credential"
            and resource.credential_ref is not None
            and resource.native is None
            and resource.resource is None
        ):
            continue
        paired = _mcp_credential_servers(source, resource)
        if paired:
            if represented and resource.credential_ref in represented.credential_refs:
                continue
            if paired & retained_mcp or resource.credential_ref in retained_credentials:
                missing.add("credentials")
            elif "mcp" in drops:
                continue
            else:
                missing.add("mcp")
                continue
        if resource.target_path is not None and "files" not in drops:
            missing.add("files")
        if (
            resource.kind == "artifact"
            and resource.resource is not None
            and resource.resource.kind == "file"
            and source.resource_scope is not None
            and (resource.resource.tenant_id, resource.resource.account_id)
            == (source.resource_scope.tenant_id, source.resource_scope.account_id)
            and resource.native is None
            and resource.credential_ref is None
            and resource.target_path is not None
        ):
            continue
        # No installed materializer proves credentials or opaque private resources.
        missing.add("credentials" if resource.kind == "credential" else "source_definition")
    if (
        (source.memory_required is not False or source.memory_store_id is not None)
        and "memory" not in drops
        and not native_retained
    ):
        from daimon.core.memory_resource import memory_mount

        retained = False
        if len(resources) == 1 and source.memory_store_id is not None:
            native, ref = resources[0].native, resources[0].resource
            retained = (
                source.source is not None
                and source.resource_scope is not None
                and source.memory_read_only is not None
                and ref is not None
                and ref.provider == source.source.ref.provider
                and ref.kind == "memory_store"
                and ref.id == source.memory_store_id
                and (ref.tenant_id, ref.account_id)
                == (source.resource_scope.tenant_id, source.resource_scope.account_id)
                and native is not None
                and (native.namespace, native.version) == ("anthropic.session_resource_create", 1)
                and native.value
                in (
                    {"resource": memory_mount(source.memory_store_id, read_only=True)},
                    {
                        "resource": memory_mount(
                            source.memory_store_id, read_only=source.memory_read_only
                        )
                    },
                )
            )
        if not retained:
            missing.add("memory")
    if source.environment is not None and source.environment.spec.native_config is not None:
        from mux.drivers.anthropic.schemas import EnvironmentConfig

        try:
            EnvironmentConfig.model_validate(dict(source.environment.spec.native_config.value))
        except ValueError:
            missing.add("source_definition")
    # Removing execution is explicit; retaining a source environment requires
    # its complete canonical policy, which native readback must also verify.
    if target is None or not (
        native_retained
        or ("sandbox" in drops and environment is None and not resources)
        or (
            source.environment is not None
            and environment is not None
            and environment == source.environment.spec
            and environment.native_config is None
        )
    ):
        missing.add("environment")
    if spec.model.options:
        missing.add("source_definition")
    return frozenset(missing)


def preview_source_drops(source: BackendAgent, model: ModelRef) -> AgentSpec:
    """Derive explicit losses using the same complete source-preservation rule."""
    spec = source.effective_spec
    profile = backend_selection(model.provider).profile
    if (
        spec is None
        or source.source is None
        or source.environment is None
        or source.resource_scope is None
        or source.memory_required is None
        or source.memory_read_only is None
        or source.asks_before_publishing is None
        or model.options
    ):
        raise UnsupportedCapability(("source_context",), profile)
    if profile == backend_selection(spec.model.provider).profile:
        target = spec.model_copy(update={"model": model})
        missing = classify_source_obligations(
            source,
            target,
            environment=source.environment.spec,
            resources=source.resource_requirements,
            retained_agent=source.source.ref,
            asks_before_publishing=source.asks_before_publishing,
        )
        if missing:
            raise UnsupportedCapability(tuple(sorted(missing)), profile)
        return target
    target = AgentSpec(
        name=spec.name,
        description=spec.description,
        model=model,
        system=spec.system,
        tools=(),
        skills=(),
        mcp_servers=(),
        metadata=spec.metadata,
    )
    representations = [item for item in source.representations if item.target.model == model]
    if representations:
        if len(representations) != 1 or representations[0].source_pin != source_preservation_pin(
            source
        ):
            raise UnsupportedCapability(("source_representation",), profile)
        target = representations[0].target
    represented = represented_source(source, target)
    environment = represented.environment if represented is not None else None
    missing = classify_source_obligations(
        source,
        target,
        environment=environment,
        asks_before_publishing=source.asks_before_publishing,
    )
    try:
        target = AgentSpec.model_validate(
            {
                **target.model_dump(),
                "declared_drops": tuple(
                    sorted("sandbox" if item == "environment" else item for item in missing)
                ),
            }
        )
    except ValueError:
        raise UnsupportedCapability(tuple(sorted(missing)), profile) from None
    missing = classify_source_obligations(
        source,
        target,
        environment=environment,
        asks_before_publishing=source.asks_before_publishing,
    )
    if missing:
        raise UnsupportedCapability(tuple(sorted(missing)), profile)
    if target.declared_drops:
        target = target.model_copy(
            update={
                "system": compose_source_instructions(
                    spec.system,
                    "Unavailable capabilities: " + ", ".join(target.declared_drops) + ".",
                )
            }
        )
    return target


def source_preservation_pin(source: BackendAgent) -> str:
    """Pin complete owner-captured intent independently of the current caller address."""
    updates: dict[str, object] = {"resource_scope": None}
    for name in ("source", "environment"):
        resource = getattr(source, name)
        if resource is not None:
            updates[name] = resource.model_copy(
                update={"ref": resource.ref.model_copy(update={"account_id": None})}
            )
    captured = source.model_copy(update=updates)
    # Direct facts include the intentionally non-serialized resource projection.
    facts = dict(captured)
    # Derived representation evidence must not change old consent pins, nor
    # recursively become part of the source it attests.
    facts.pop("representations", None)
    return request_digest(cast(JsonValue, TypeAdapter(object).dump_python(facts, mode="json")))


def represented_source(
    source: BackendAgent, target: AgentSpec | None
) -> SourceRepresentation | None:
    if target is None:
        return None
    matches = [
        item
        for item in source.representations
        if item.source_pin == source_preservation_pin(source)
        and item.target.model == target.model
        and item.target.tools == target.tools
        and item.target.skills == target.skills
        and item.target.mcp_servers == target.mcp_servers
        and item.target.metadata == target.metadata
        and item.target.name == target.name
        and item.target.description == target.description
        and item.target.extensions == target.extensions
        and source_instructions_preserved(item.target.system, target.system)
    ]
    return matches[0] if len(matches) == 1 else None


async def prepare_source(request: ProviderPreparationRequest) -> ProviderPreparationRequest:
    """Prepare scoped source intent; native output verification remains mandatory."""
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.ma_identity import derive_agent_uuid

    revision = request.admission.backend_revision
    if (
        request.admission.backend_revision_origin != "agent"
        or not request.deps.agent_backends
        or not request.deps.channel_backends
        or request.deps.turn_path != "mux"
        or revision is None
        or request.scope.tenant_id != str(request.tenant_id)
        or request.scope.account_id != str(request.session_account_id)
        or request.scope.account_id != str(request.admission.account_id)
        or request.scope.is_platform
        or request.scope.is_legacy_host_authorized
    ):
        raise AdmissionDenied(reason="backend_unsupported")

    request = replace(
        request,
        deadline=turn_deadline(
            now=request.now(),
            deadline=request.deadline,
            authority_deadline=request.worker.limits.deadline
            if request.worker is not None
            else None,
        ),
    )

    agent_id = derive_agent_uuid(
        tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
    )
    setting = None
    if request.worker is None:
        from daimon.core.stores import agent_backends, mux_state

        async with asyncio.timeout(
            remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        ):
            async with request.deps.sessionmaker() as session:
                with session.no_autoflush:
                    setting = await mux_state.get_config_agent_revision(
                        session,
                        revision,
                        agent_id=agent_id,
                    )
                    provisional = request.admission.backend_agent_revision
                    if provisional is not None:
                        if (
                            await mux_state.get_config_revision(
                                session, revision.channel, revision.local
                            )
                            is None
                        ):
                            setting = await agent_backends.current(
                                session, request.tenant_id, agent_id, persisted=True
                            )
                        if (
                            setting != provisional
                            or setting is None
                            or setting.drop_consent is None
                        ):
                            raise AgentBackendRefused("re_confirm")
    timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
    source = await asyncio.wait_for(
        capture_preparation_source(request, fresh=True)
        if setting is not None
        else capture_preparation_source(request),
        timeout=timeout,
    )
    if (
        source.tenant_id != request.tenant_id
        or source.agent_id != agent_id
        or source.resource_scope != request.scope
        or source.source is None
        or source.source.ref.kind != "agent"
        or source.source.ref.tenant_id != request.scope.tenant_id
        or source.source.lifecycle != "active"
        or source.source.ref.id != request.admission.agent.id
        or source.source.revision.local != request.admission.agent.version
    ):
        if setting is not None:
            raise AgentBackendRefused("re_confirm")
        raise UnsupportedCapability(("source_context",), revision.profile)
    # Capture is evidence of source intent, not evidence that a target preserved it.
    spec = source.effective_spec
    if spec is None:
        raise UnsupportedCapability(("source_definition",), revision.profile)
    worker = request.worker
    if worker is not None:
        fleet = worker.permissions.fleet
        if (
            fleet.scope != request.scope
            or not fleet.enabled
            or not fleet.active
            or fleet.depth != 1
            or fleet.account_id != request.admission.account_id
            or fleet.root.channel != revision.channel
            or (worker.source_agent is None and worker.permissions.agent_id != source.agent_id)
            or worker.backend.profile != revision.profile
            or worker.backend.backend != revision.backend
            or worker.backend.model != revision.model
            or dict(worker.backend.requires) != dict(revision.requires)
            or worker.backend.thread_mode != revision.thread_mode
            or worker.permissions.spec != worker.spec
            or worker.permissions.resources != worker.resources
            or worker.permissions.environment != worker.environment
            or worker.permissions.limits != worker.limits
            or worker.permissions.subject.is_admin
            or (request.admission.memory_read_only and not worker.permissions.memory_read_only)
            or not request.admission.origin_seal_ids <= worker.permissions.seal_ids
            or (
                worker.source_agent is not None
                and worker.source_agent != (source.source.ref, source.source.revision)
            )
        ):
            raise UnsupportedCapability(("worker_context",), revision.profile)
    target = worker.spec if worker is not None else None
    if setting is not None:
        consent = setting.drop_consent
        if consent is None or setting.backend != revision.backend:
            raise AgentBackendRefused("re_confirm")
        if revision.model is None and revision.backend != spec.model.provider:
            raise AgentBackendRefused("re_confirm")
        if source_preservation_pin(source) != consent.source_pin:
            raise AgentBackendRefused("re_confirm")
        target = preview_source_drops(
            source, ModelRef(provider=revision.backend, id=revision.model or spec.model.id)
        )
        if (
            target.declared_drops != consent.declared_drops
            or request_digest(target.model_dump(mode="json")) != consent.target_digest
        ):
            raise AgentBackendRefused("re_confirm")
    retain_native = (
        worker is None
        and setting is not None
        and not setting.declared_drops
        and revision.profile == backend_selection(spec.model.provider).profile
    )
    missing = classify_source_obligations(
        source,
        target,
        retained_agent=source.source.ref if retain_native else None,
        environment=source.environment.spec
        if retain_native and source.environment
        else worker.environment
        if worker
        else represented.environment
        if (represented := represented_source(source, target)) is not None
        else None,
        resources=source.resource_requirements
        if retain_native
        else worker.resources
        if worker
        else (),
        asks_before_publishing=request.admission.asks_before_publishing,
        worker_ceiling=worker is not None,
    )
    if missing:
        raise UnsupportedCapability(tuple(sorted(missing)), revision.profile)
    return replace(request, source=source, target_spec=target)


async def read_preparation_counterpart(
    request: ProviderPreparationRequest,
) -> ProviderPreparationRequest:
    """Verify the prepared source's owned native counterpart before handoff."""

    from daimon.core.channel_backend import AgentBackendRefused

    try:
        selected = await prepare_source(request)
        request = replace(request, deadline=selected.deadline)
        source, revision, worker = (
            selected.source,
            selected.admission.backend_revision,
            selected.worker,
        )
        if source is None or revision is None:
            raise AdmissionDenied(reason="backend_unsupported")
        target = selected.target_spec
        from daimon.core.turn.runtimes import ScopedTurnRuntimes

        if isinstance(selected.deps.turn_runtimes, ScopedTurnRuntimes):
            selected.deps.turn_runtimes.bind_preparation(selected)
        runtime = selected.deps.turn_runtimes.get(revision.profile)
        if runtime is None or runtime.read_agent_counterpart is None:
            raise UnsupportedCapability(("native_counterpart",), revision.profile)
        if (
            worker is not None
            and runtime.materialize_worker is not None
            and selected.before_native is not None
        ):
            from daimon.core.turn.persistence import current_persistence

            persistence = current_persistence.get()
            if (
                persistence is None
                or persistence.store is not selected.deps.state_store
                or persistence.slot.thread.channel != revision.channel
                or persistence.slot.thread.thread_id != selected.thread_id
                or persistence.slot.account_id != selected.scope.account_id
            ):
                raise UnsupportedCapability(("worker_materialization",), revision.profile)
            persistence.check_session(selected.scope, None)
            timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
            await asyncio.wait_for(selected.before_native(), timeout=timeout)
            timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
            allocated = await asyncio.wait_for(
                runtime.materialize_worker(selected), timeout=timeout
            )
            selected = replace(selected, counterpart=allocated)
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        counterpart = await asyncio.wait_for(
            runtime.read_agent_counterpart(selected), timeout=timeout
        )
        if selected.counterpart is not None and counterpart != selected.counterpart:
            raise UnsupportedCapability(("worker_acknowledgement",), revision.profile)
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        current_source = await asyncio.wait_for(
            capture_preparation_source(request, fresh=True)
            if worker is None and target is not None
            else capture_preparation_source(request),
            timeout=timeout,
        )
        remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        if worker is None and target is not None and current_source != source:
            raise AgentBackendRefused("re_confirm")
        if target is None or counterpart.agent.spec != target or current_source != source:
            raise UnsupportedCapability(("source_drift",), revision.profile)
        return replace(selected, counterpart=counterpart, verified_counterpart=counterpart)
    except (UnsupportedCapability, TimeoutError, TurnError, AgentBackendRefused) as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    except Exception:
        raise AdmissionDenied(reason="backend_unsupported") from None


class CounterpartPlan(Protocol):
    """Read-only operator pins shared by native deployment plans."""

    @property
    def profile(self) -> str: ...
    @property
    def worker_materialization(self) -> bool: ...
    @property
    def tenant_id(self) -> str: ...
    @property
    def account_id(self) -> str: ...
    @property
    def platform(self) -> str: ...
    @property
    def channel_id(self) -> str: ...
    @property
    def project(self) -> str: ...
    @property
    def agent_id(self) -> str | None: ...
    @property
    def skill_ids(self) -> tuple[str, ...]: ...
    @property
    def source_agent_id(self) -> UUID | None: ...
    @property
    def source_agent_version(self) -> int | None: ...
    @property
    def counterpart_catalog_id(self) -> str | None: ...
    @property
    def counterpart_catalog_revision(self) -> int | None: ...
    @classmethod
    def model_validate(cls, obj: object) -> Self: ...


def validate_counterpart_pins(
    plan: CounterpartPlan, *, worker_materialization: bool = False
) -> None:
    """Legacy rows have no counterpart; partial source/catalog pins never authorize one."""
    if worker_materialization:
        if (
            plan.source_agent_id is None
            or plan.source_agent_version is None
            or plan.counterpart_catalog_id is not None
            or plan.counterpart_catalog_revision is not None
            or plan.agent_id is not None
        ):
            raise ValueError(
                "worker materialization requires source pins without fixed target pins"
            )
        return
    pins = (
        plan.source_agent_id,
        plan.source_agent_version,
        plan.counterpart_catalog_id,
        plan.counterpart_catalog_revision,
    )
    if any(pin is not None for pin in pins) and any(pin is None for pin in pins):
        raise ValueError("a counterpart requires complete source and catalog revision pins")
    if all(pin is not None for pin in pins) and plan.agent_id is None:
        raise ValueError("a counterpart requires a native agent identity")


def select_runtime_manifest[Plan: CounterpartPlan](
    request: ProviderPreparationRequest, plan_type: type[Plan]
) -> Plan:
    """Select exactly one installed scope/configuration grant before credential discovery."""

    revision = request.admission.backend_revision
    if revision is None:
        raise AdmissionDenied(reason="backend_unsupported")
    manifest_path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not manifest_path:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        with Path(manifest_path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise AdmissionDenied(reason="backend_unsupported")
        selected = [
            row
            for row in _ROWS.validate_json(raw)
            if row.get("profile") == revision.profile
            and row.get("tenant_id") == request.scope.tenant_id
            and row.get("platform") == revision.channel.platform
            and row.get("channel_id") == revision.channel.channel_id
            and row.get("account_id") == request.scope.account_id
            and manifest_configuration_matches(row, revision)
        ]
        if len(selected) != 1:
            raise AdmissionDenied(reason="backend_unsupported")
        return plan_type.model_validate(selected[0])
    except (OSError, ValidationError):
        raise AdmissionDenied(reason="backend_unsupported") from None


def validate_counterpart_request(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    worker_materialization: bool = False,
) -> ConfigRevision:
    """Validate the same immutable source/configuration pins before native allocation."""
    from daimon.core.ma_identity import derive_agent_uuid

    try:
        validate_counterpart_pins(plan, worker_materialization=worker_materialization)
    except ValueError:
        raise AdmissionDenied(reason="backend_unsupported") from None
    revision, scope = request.admission.backend_revision, request.scope
    if (
        request.admission.backend_revision_origin != "agent"
        or revision is None
        or not request.deps.channel_backends
        or request.deps.turn_path != "mux"
        or scope.tenant_id != str(request.tenant_id)
        or scope.account_id != str(request.admission.account_id)
        or scope.account_id != str(request.session_account_id)
        or (plan.tenant_id, plan.account_id) != (scope.tenant_id, scope.account_id)
        or (revision.channel.tenant_id, revision.channel.platform, revision.channel.channel_id)
        != (plan.tenant_id, plan.platform, plan.channel_id)
        or request.platform != plan.platform
        or revision.profile != plan.profile
        or plan.source_agent_id
        != derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id)
        or plan.source_agent_version != request.admission.agent.version
        or (worker_materialization and (request.worker is None or request.before_native is None))
        or (
            not worker_materialization
            and (
                plan.counterpart_catalog_id is None
                or plan.counterpart_catalog_revision is None
                or plan.agent_id is None
            )
        )
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if select_runtime_manifest(request, type(plan)) != plan:
        raise AdmissionDenied(reason="backend_unsupported")
    return revision


async def materialize_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    original: ProviderPreparationRequest,
    *,
    resources: Callable[[TurnPersistence, ResolvedWorkerSpec], Awaitable[NeutralResourceHost]],
) -> AgentRevision:
    """Publish through the held preparation owner after validating its original grant."""
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.persistence import current_persistence

    validate_counterpart_request(plan, request, worker_materialization=True)
    worker, source, guard = request.worker, request.source, request.before_native
    active = current_persistence.get()
    if (
        not plan.worker_materialization
        or (request.scope, request.admission.backend_revision, worker)
        != (original.scope, original.admission.backend_revision, original.worker)
        or worker is None
        or source is None
        or source.source is None
        or guard is None
        or active is None
        or active.scope != request.scope
        or active.store is not request.deps.state_store
        or request.deps.state_store is not original.deps.state_store
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    active.check_session(request.scope, None)
    host = await resources(active, worker)
    catalog_id = active.operation_key + ":agent"
    return await host.publish(
        catalog_id,
        worker.spec,
        expected_revision=0,
        key=catalog_id,
        persistence=active,
        guard=guard,
        worker=worker,
        source=source.source,
    )


async def read_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    source: BackendAgent,
    resources: NeutralResourceHost,
    verify_native: Callable[[AgentRevision], Awaitable[None]],
    worker_materialization: bool = False,
) -> AgentRevision:
    """Verify an installed native catalog component, without authorizing execution.

    Complete source preservation is a separate owner's proof. Names and source
    digests cannot substitute for identity, version or operator configuration.
    Readback detects drift; it does not create a native CAS guarantee.
    """
    from daimon.core.skills.ingest import SkillIngestError
    from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
    from mux.profiles import get_profile

    revision = validate_counterpart_request(
        plan, request, worker_materialization=worker_materialization
    )
    scope = request.scope
    from daimon.core.turn.persistence import current_persistence

    persistence = current_persistence.get() if worker_materialization else None
    acknowledged = request.counterpart if worker_materialization else None
    if worker_materialization and (
        persistence is None
        or persistence.store is not request.deps.state_store
        or acknowledged is None
        or source.source is None
        or source.resource_scope != scope
        or request.worker is None
        or request.worker.permissions.fleet.scope != scope
        or persistence.slot.thread.channel != revision.channel
        or persistence.slot.thread.thread_id != request.thread_id
        or persistence.slot.account_id != scope.account_id
        or acknowledged.catalog_id != persistence.operation_key + ":agent"
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if persistence is not None:
        persistence.check_session(scope, None)
    catalog_id = acknowledged.catalog_id if acknowledged else cast(str, plan.counterpart_catalog_id)
    if (
        source.tenant_id != request.tenant_id
        or source.agent_id != plan.source_agent_id
        or resources.scope != scope
        or resources.workspace != plan.project
        or resources.backend.capabilities().profile_id != plan.profile
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        row = await resources.entry(catalog_id)
        if (
            (acknowledged is not None and row != acknowledged)
            or (
                not worker_materialization
                and (
                    row.local_revision != plan.counterpart_catalog_revision
                    or row.agent.ref.id != plan.agent_id
                )
            )
            or row.agent.spec.model.id != revision.model
            or (
                request.admission.backend_counterpart is not None
                and row != request.admission.backend_counterpart
            )
            or row.agent.spec.model.provider != get_profile(plan.profile).provider
            or tuple(pin.id for pin in row.agent.spec.skills or ()) != plan.skill_ids
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        checked = (
            await resources.read(
                catalog_id,
                store=persistence.store,
                key=persistence.operation_key + ":agent_create:0",
                worker=request.worker,
                source=source.source,
            )
            if persistence is not None
            else await resources.read(catalog_id)
        )
        await verify_native(checked)
        if (
            checked != row
            or select_runtime_manifest(request, type(plan)) != plan
            or await resources.entry(catalog_id) != row
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        return checked
    except (
        ValueError,
        KeyError,
        SkillIngestError,
        ProviderError,
        ScopeViolation,
        UnsupportedCapability,
    ):
        raise AdmissionDenied(reason="backend_unsupported") from None
