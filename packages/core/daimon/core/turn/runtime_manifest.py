"""Credential-reference validation without credential discovery or SDK imports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Final,
    Literal,
    Protocol,
    Self,
    cast,
    get_type_hints,
    overload,
)
from uuid import UUID

import structlog
from daimon.core.caller_resources import (
    APP_SESSION_VAULT_PREFIX,
    CALLER_RESOURCES_ID,
    CALLER_VAULT_PREFIX,
)
from daimon.core.errors import DaimonError
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
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    ProviderBinding,
    ResourceBinding,
    ToolSpec,
)
from mux.errors import InvalidConfig, MuxError, UnsupportedCapability
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
    from daimon.core.stores.domain import (
        BackendAgent,
        SourceRepresentation,
        TaskContinuationRow,
        _BackendDropConsent,  # pyright: ignore[reportPrivateUsage]
    )
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
_WILDCARD = "*"
#: Built-in web tools an agent template never carries off Anthropic: they are named
#: losses (OpenAI's web_search is unpriced and uncounted; Gemini's grounding is off).
_WEB_TOOLS = frozenset({"web_search", "web_fetch"})
"""Reserved sentinel for 'every value' on `platform`, `channel_id` and
`account_id` only. Never accepted for `tenant_id` or `profile`: a manifest
row always authorizes resources for exactly one tenant, so the one hard
scope boundary (`tenant_id`) and the chosen native profile stay exact-match
always. This lets one row authorize "this tenant's OpenAI resources for
every channel/account", the deployment-wide default self-host operators
need, without ever letting a row serve a different tenant."""
_WILDCARDABLE_FIELDS = ("platform", "channel_id", "account_id")
_ADMISSION_FACTS = {
    name: TypeAdapter(annotation)
    for name, annotation in get_type_hints(Admission).items()
    if name
    not in ("agent", "environment", "observation", "backend_counterpart", "backend_agent_revision")
}

#: Admission facts a root retained before they existed read as their default.
_LEGACY_ADMISSION_FACTS = frozenset({"daimon_rule_tools", "daimon_rules_need_driver"})

log = structlog.get_logger(__name__)


async def read_agent_fleet_grant(
    deps: TurnDeps, admission: Admission, *, now: datetime
) -> AgentFleetGrant | None:
    """The fleet the actual admitted source may use now, without name lookup.

    Its own grant when it has one; otherwise the computed default, unless fleet
    was turned off for it or the turn's platform doesn't run fleets yet
    (`FLEET_LIFECYCLE_PLATFORMS`). `FLEET__ENABLED=false` stops all of them.
    """
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.fleet_configuration import FLEET_LIFECYCLE_PLATFORMS
    from daimon.core.ma_identity import derive_agent_uuid

    if not deps.fleet_enabled:
        return None
    if admission.grant is None:
        raise WorkerSpecRefused("origin", "unbound")
    setting = await AgentBackendService(deps.sessionmaker, enabled=True).fleet_setting(
        admission.grant.tenant_id,
        derive_agent_uuid(tenant_id=admission.grant.tenant_id, ma_agent_id=admission.agent.id),
        now=now,
    )
    if setting.kind == "default" and admission.grant.platform not in FLEET_LIFECYCLE_PLATFORMS:
        # The default fleet only where the adapter settles and delivers workers.
        return None
    return setting.grant


async def default_fleet_unavailable_here(
    deps: TurnDeps,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    platform: str | None,
    now: datetime,
) -> bool:
    """Whether this agent has only the default fleet and this platform doesn't run it.

    A custom grant is a different story (its root may just have gone stale), so it is
    never reported as "not available here".
    """
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.fleet_configuration import FLEET_LIFECYCLE_PLATFORMS

    if platform in FLEET_LIFECYCLE_PLATFORMS:
        return False
    setting = await AgentBackendService(deps.sessionmaker, enabled=True).fleet_setting(
        tenant_id, agent_id, now=now
    )
    return setting.kind == "default"


async def read_agent_fleet_grant_or_off(
    deps: TurnDeps, admission: Admission, *, now: datetime
) -> AgentFleetGrant | None:
    """`read_agent_fleet_grant` for a chat turn: a failed read is fleet off, not a failed turn.

    Refusals (an unbound origin, an invalid stored grant) still raise. Anything else
    (the database, a timeout) is logged and read as no fleet: nothing is retained.
    """
    from daimon.core.channel_backend import AgentBackendRefused

    try:
        return await read_agent_fleet_grant(deps, admission, now=now)
    except (WorkerSpecRefused, AgentBackendRefused):
        raise
    except Exception as error:  # a fleet read never fails a chat turn
        log.error("fleet.read_failed", error_type=type(error).__name__)
        return None


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
            # Epoch 0 is the computed default; the digest check below binds it.
            or root.grant_epoch is None
            or root.grant_epoch < 0
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
        or (account.tenant_id, account.platform) != (origin.tenant_id, origin.platform)
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
            setting = await AgentBackendService(deps.sessionmaker, enabled=True).fleet_setting(
                UUID(scope.tenant_id), agent_id, now=now
            )
        except AgentBackendRefused:
            raise WorkerSpecRefused("origin", "unsupported") from None
        value = setting.grant
        if parent is None:
            # A turn under the default fleet may run without a root (it answers with
            # fleet off); a custom grant's turn always retains one.
            if setting.kind == "custom" or origin.is_setup:
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
        or request.admission.hold is not None
        or request.session_account_id != request.admission.account_id
        or not any(
            backend.profile == profile and selected.id == model
            for backend, selected in value.choices
        )
    ):
        raise WorkerSpecRefused("native_budget_usd", "unsupported")
    # A provider without a native session cap (OpenAI on our project) gets the same
    # host-side controls and cash hold, and no native budget it can't honour.
    native_budget = (
        value.native_root_budget_usd
        if get_profile(profile).support.get("session_spend_control") == "native"
        else None
    )
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
        return replace(request, native_budget_usd=native_budget)
    if binding is not None or origin_id is None or intent_id is None:
        raise WorkerSpecRefused("origin", "unbound")
    deadline = min(request.deadline, value.expires_at)
    remaining_s(deadline, now=request.now(), refuse_expired=True)
    controls: dict[str, JsonValue] = {
        "origin_id": str(origin_id),
        "grant_digest": request_digest(value.model_dump(mode="json")),
        "deadline": deadline.isoformat(),
        "max_inputs": min(3, value.max_root_turns),
        # Without a native cap the grant's figure only sizes the cash hold: say so.
        ("native_budget_usd" if native_budget is not None else "hold_budget_usd"): str(
            value.native_root_budget_usd
        ),
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
        native_budget_usd=native_budget,
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
        # The guard reads the current grant on its own sessions: run it before taking the
        # tenant fleet lock, which every fleet admission and budget read in the tenant
        # waits on for as long as this send claim holds it (SMOKE J4).
        await guard()
        root = await require_root_funding(
            db, scope, fence.slot, origin_id=origin_id, controls=controls, markup=deps.markup
        )
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


#: Why the latest turns couldn't retain a fleet root, by origin. In-process and
#: bounded: another replica, or a restart, falls back to the generic refusal.
_ROOT_REFUSALS: OrderedDict[UUID, str] = OrderedDict()
_ROOT_REFUSALS_MAX = 1024


def _remember_root_refusal(origin_id: UUID, obstacle: str) -> None:
    _ROOT_REFUSALS[origin_id] = obstacle
    _ROOT_REFUSALS.move_to_end(origin_id)
    while len(_ROOT_REFUSALS) > _ROOT_REFUSALS_MAX:
        _ROOT_REFUSALS.popitem(last=False)


def root_refusal(origin_id: UUID) -> str | None:
    """The obstacle that kept this origin's turn from retaining a fleet root, if known."""
    return _ROOT_REFUSALS.get(origin_id)


async def freeze_prepared_root(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    intent_id: UUID,
    now: Callable[[], datetime],
) -> None:
    """Retain the exact host-owned root under its input's existing mutation fence.

    Under an agent's own grant a refusal fails the turn, as it always has. Under
    the computed default (every agent, nothing configured), a turn that can't
    become a fleet root is an ordinary turn: it answers with fleet off and the
    fleet tools say why. The default never makes a chat turn fail.
    """
    from daimon.core.fleet_configuration import is_default_fleet
    from daimon.core.neutral_resources import fleet_root_obstacle
    from daimon.core.turn_origin import current_origin_id

    try:
        value = await read_agent_fleet_grant(deps, prepared.admission, now=now())
    except Exception as error:
        from daimon.core.channel_backend import AgentBackendRefused

        if isinstance(error, (WorkerSpecRefused, AgentBackendRefused)):
            raise
        # The grant can't be read: retain nothing; the turn answers with fleet off.
        log.error("fleet.read_failed", error_type=type(error).__name__, at="freeze")
        if (origin_id := current_origin_id.get()) is not None:
            _remember_root_refusal(origin_id, "unavailable")
        return
    if value is None:
        if prepared._source is not None or prepared._preparation is not None:  # pyright: ignore[reportPrivateUsage]
            raise WorkerSpecRefused("origin", "stale")
        return
    if not is_default_fleet(value):
        await _freeze_root(deps, prepared, value, intent_id=intent_id, now=now)
        return
    try:
        await _freeze_root(deps, prepared, value, intent_id=intent_id, now=now)
    except Exception as error:
        # Any failure (a refusal, a conflict, the database) only turns fleet off for this
        # turn. Cancellation is a BaseException and still propagates.
        source = prepared._source  # pyright: ignore[reportPrivateUsage]
        if prepared.admission.hold is not None:
            obstacle = "held"
        elif prepared.admission.shared_owner is not None:
            obstacle = "shared_thread"
        else:
            obstacle = (fleet_root_obstacle(source) if source is not None else None) or (
                "unavailable"
            )
        from daimon.core.turn.persistence import BindingSuperseded
        from mux.state.lease import StaleFence

        # A lost fence or binding means another writer moved on: worth an error line.
        (log.error if isinstance(error, (StaleFence, BindingSuperseded)) else log.warning)(
            "fleet.default_root_skipped",
            # One stable field to group by: the setup obstacle, else the error's class.
            cause=obstacle if obstacle != "unavailable" else type(error).__name__,
            error_type=type(error).__name__,
            field=getattr(error, "field", None),
            reason=getattr(error, "reason", None),
            obstacle=obstacle,
        )
        if (origin_id := current_origin_id.get()) is not None:
            _remember_root_refusal(origin_id, obstacle)


def _counterpart_admitted(admission: Admission, scope: Scope) -> bool:
    """A root that answers through a native counterpart (a GPT or Gemini agent) can
    coordinate a fleet: its counterpart must be the one admission verified for this
    turn, in this tenant and account, on the admitted backend revision's provider and
    model, active.

    The logical agent is what the fleet freezes; the counterpart is only how it runs.
    """
    counterpart, revision = admission.backend_counterpart, admission.backend_revision
    if counterpart is None:
        return True
    agent = counterpart.agent
    return (
        revision is not None
        and agent.ref.tenant_id == scope.tenant_id
        and agent.ref.account_id == scope.account_id
        and agent.spec.model.id == revision.model
        and agent.spec.model.provider == revision.backend
        and agent.lifecycle == "active"
        and agent.archived_at is None
    )


async def _freeze_root(
    deps: TurnDeps,
    prepared: PreparedTurn,
    value: AgentFleetGrant,
    *,
    intent_id: UUID,
    now: Callable[[], datetime],
) -> None:
    from uuid import uuid5

    from daimon.core.authz import FleetFacts
    from daimon.core.mux_compat import source_environment_record
    from daimon.core.neutral_resources import fleet_root_permissions
    from daimon.core.session_compat import MaCapabilities
    from daimon.core.stores import fleet
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.turn.persistence import current_persistence
    from daimon.core.turn_origin import current_origin_id
    from mux.state.operations import request_digest

    persistence, origin_id = current_persistence.get(), current_origin_id.get()
    grant, source = prepared.admission.grant, prepared._source  # pyright: ignore[reportPrivateUsage]
    if not (persistence and origin_id and grant and source):
        raise WorkerSpecRefused("origin", "unbound")
    scope, binding = persistence.scope, persistence.binding
    if (
        not deps.fleet_enabled
        or prepared.admission.shared_owner is not None
        or prepared.admission.hold is not None
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
    if request is None or not _counterpart_admitted(prepared.admission, scope):
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
            or admission.hold != prepared.admission.hold
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
    try:
        permissions = fleet_root_permissions(
            source,
            grant,
            value,
            fleet=FleetFacts(
                scope=scope,
                root=binding.thread,
                origin_id=origin_id,
                account_id=prepared.admission.account_id,
                # A placeholder, replaced by the run's id; derived, so a root draws no
                # random id (shared legacy/mux goldens replay the same id sequence).
                run_id=uuid5(origin_id, "fleet-root-placeholder"),
                place=grant.run_place,
                enabled=True,
                active=True,
            ),
            policy=policy,
            now=now(),
            seal_ids=prepared.admission.origin_seal_ids,
            # A GPT/Gemini root runs on its counterpart's model: that is its own model.
            execution_model=(
                ModelRef(
                    provider=prepared.admission.backend_revision.backend,
                    id=prepared.admission.backend_revision.model,
                )
                if prepared.admission.backend_counterpart is not None
                and prepared.admission.backend_revision is not None
                and prepared.admission.backend_revision.model is not None
                else None
            ),
        )
    except WorkerSpecRefused as error:
        # Only when the agent's own setup can't become worker authority (e.g. a
        # connected tool with its own login, or its very first turn, before its
        # memory store exists) does the turn run with fleet off: nothing is
        # retained and the fleet tools say why. Any other refusal (policy, grant,
        # backend, budget, identity) still fails the turn as before.
        from daimon.core.neutral_resources import fleet_root_obstacles

        obstacles = fleet_root_obstacles(source)
        obstacle = obstacles[0] if obstacles else None
        if obstacle is None and error.field == "memory" and source.memory_store_id is None:
            obstacle = "memory"
        if obstacle is None:
            raise
        log.warning(
            "fleet.root_freeze_refused",
            cause=obstacle,
            field=error.field,
            reason=error.reason,
            obstacle=obstacle,
            # Every obstacle, not only the one shown: one can mask another.
            obstacles=list(obstacles),
            origin_id=str(origin_id),
        )
        _remember_root_refusal(origin_id, obstacle)
        return
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
    from daimon.core.turn.prepare import admitted_session_scope
    from daimon.core.turn_origin import current_origin_id

    def refuse(reason: str) -> WorkerSpecRefused:
        # One closed, log-safe name per failed condition; the refusal itself is unchanged.
        log.warning("fleet.resume_root_refused", reason=reason, continuation_id=str(row.id))
        return WorkerSpecRefused("origin", "unbound")

    persistence, origin_id = current_persistence.get(), current_origin_id.get()
    source, grant = prepared._source, prepared.admission.grant  # pyright: ignore[reportPrivateUsage]
    for missing, reason in (
        (not deps.fleet_enabled, "fleet_disabled"),
        (persistence is None, "no_persistence"),
        (origin_id is None, "no_origin"),
        (source is None, "no_captured_source"),
        (grant is None, "no_grant"),
    ):
        if missing:
            raise refuse(reason)
    assert persistence is not None and source is not None and grant is not None
    # An ordinary Anthropic turn carries no prepared scope: its persistence adopts
    # the admitted scope, exactly as the root's first turn froze it.
    expected_scope = (
        prepared._scope  # pyright: ignore[reportPrivateUsage]
        or admitted_session_scope(
            prepared.admission, tenant_id=row.tenant_id, session_id=prepared.ma_session_id
        )
    )
    for failed, reason in (
        (prepared.admission.shared_owner is not None, "shared_owner"),
        (prepared.admission.hold is not None, "held_turn"),
        (expected_scope != persistence.scope, "prepared_scope"),
        (source.resource_scope != persistence.scope, "source_scope"),
        (row.tenant_id != UUID(persistence.scope.tenant_id), "tenant"),
        (row.requester_account_id != UUID(persistence.scope.account_id), "requester_account"),
        (prepared.session_account_id != prepared.admission.account_id, "session_account"),
        (grant.subject.platform_user_id != row.requester_external_user_id, "grant_subject"),
        (row.started_at is None, "not_started"),
        (not row.lease_owner, "no_lease"),
    ):
        if failed:
            raise refuse(reason)
    # Narrowing only: each case was refused above.
    assert origin_id is not None and row.lease_owner is not None and row.started_at is not None

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
            if not isinstance(admission_facts, dict) or not (
                set(_ADMISSION_FACTS) - _LEGACY_ADMISSION_FACTS
                <= set(admission_facts)
                <= set(_ADMISSION_FACTS)
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
                    if name in admission_facts
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

    def __init__(
        self,
        reason: Literal["reserved_key_reference", "invalid_manifest", "wildcard_tenant_or_profile"],
    ) -> None:
        self.reason = reason
        if reason == "reserved_key_reference":
            message = (
                "Provider runtime api_key_env uses a reserved provider prefix "
                "(OPENAI_, ANTHROPIC_, GOOGLE_, GEMINI_). Use a host-scoped name such as "
                "DAIMON_CHANNEL_OPENAI_KEY or DAIMON_CHANNEL_GEMINI_KEY."
            )
        elif reason == "wildcard_tenant_or_profile":
            message = (
                'Provider runtime manifest rows may use "*" on platform, channel_id or '
                'account_id only. "*" is never valid for tenant_id or profile.'
            )
        else:
            message = (
                "Provider runtime manifest must be a readable JSON list of objects, at most 1 MiB."
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
        if row.get("tenant_id") == _WILDCARD or row.get("profile") == _WILDCARD:
            raise ProviderRuntimeConfigurationError("wildcard_tenant_or_profile")


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


@dataclass(frozen=True)
class _TemplateInputs:
    """Capture inputs a template row implies: only Daimon MCP, from the live catalogue."""

    source_pin: str
    model: ModelRef
    mcp_tools: tuple[str, ...] = ()
    skills: tuple[SourceSkillMapping, ...] = ()
    environment: EnvironmentSpec | None = None


def _grant_models(row: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The models a template row's configuration grant names; none for a malformed grant."""
    grant = row.get("configuration_grant")
    models = grant.get("models") if isinstance(grant, dict) else None
    return tuple(m for m in models if isinstance(m, str)) if isinstance(models, list) else ()


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
    from mux.errors import ScopeViolation

    if request.admission.hold is not None:
        # The source records the agent-wide memory store; a held turn mounts its
        # channel's store only, so it never has a source to capture.
        raise ScopeViolation("source-preparation", "a held turn never captures a fleet source")
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
        hide_personal_for=request.admission.shared_owner,
        slack_turn_context_id=request.admission.slack_turn_context_id,
        hold=None if request.admission.shared_owner is not None else request.admission.hold,
        private_dm_id=request.admission.private_dm_id,
        origin_seal_ids=request.admission.origin_seal_ids,
    )
    return await _capture_source_capabilities(
        request, captured, capability_preview=capability_preview
    )


#: A connector name a declared `mcp_server:<name>` loss can spell (mux contracts).
_CONNECTOR_NAME = re.compile(r"[A-Za-z0-9_.-]+")


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
    from daimon.core.turn.runtime_defaults import capture_template_rows
    from mux.contracts.resources import MCPConnection, SkillUpload, SkillUploadFile
    from mux.drivers.anthropic import (
        AGENT_TOOLS_NAMESPACE,
        AgentToolsConfig,
        MCPToolset,
        agent_tools,
        mcp_permissions,
    )
    from mux.profiles import represented_execution_tools
    from sqlalchemy.exc import SQLAlchemyError

    source = source.model_copy(update={"representations": ()})
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    rows: list[dict[str, JsonValue]] = []
    if path is not None:
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
    # A code-default agent template counts only where the operator configured no
    # row for this tenant's profile at all, matching selection's last resort.
    tenant = str(source.tenant_id)
    operator_profiles = {str(row.get("profile")) for row in rows if row.get("tenant_id") == tenant}
    rows = [
        *rows,
        *(
            row
            for row in capture_template_rows(tenant)
            if str(row.get("profile")) not in operator_profiles
        ),
    ]
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
        templates = [
            (row, model)
            for row in rows
            if row.get("root_materialization") is True
            and row.get("template_capabilities") == "mcp"
            and row.get("tenant_id") == str(source.tenant_id)
            and row.get("platform") in (request.platform, _WILDCARD)
            and row.get("channel_id") in (request.admission.origin_channel_id, _WILDCARD)
            and row.get("account_id") in (request.scope.account_id, _WILDCARD)
            and (
                capability_preview
                or revision is not None
                and row.get("profile") == revision.profile
            )
            for model in _grant_models(row)
            if capability_preview or revision is not None and model == revision.model
        ]
        if not selected and not templates:
            return source
        spec, scope = source.effective_spec, source.resource_scope
        if spec is None or scope is None or scope != request.scope:
            raise ValueError("missing captured source")
        pin = source_preservation_pin(source)
        represented: list[SourceRepresentation] = []
        candidates: list[tuple[dict[str, JsonValue], SourceCapabilities | _TemplateInputs]] = []
        for row in selected:
            candidates.append((row, SourceCapabilities.model_validate(row["source_capabilities"])))
        for row, model in templates:
            candidates.append(
                (
                    row,
                    _TemplateInputs(
                        source_pin=pin,
                        model=ModelRef(
                            provider=get_profile(str(row.get("profile"))).provider, id=model
                        ),
                    ),
                )
            )
        for row, inputs in candidates:
            template = isinstance(inputs, _TemplateInputs)
            profile_id = row.get("profile")
            if not isinstance(profile_id, str):
                raise ValueError("missing representation profile")
            profile = get_profile(profile_id)
            if (
                inputs.source_pin != pin
                or profile.provider != inputs.model.provider
                or profile.skill_digest not in ("version", "content")
                or not template
                and row.get("platform") != request.platform
                or not template
                and row.get("channel_id") != request.admission.origin_channel_id
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
                    surface="provider_turn",
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
            if template:
                # A template carries no operator list: the live catalogue is the source's.
                inputs = _TemplateInputs(
                    source_pin=inputs.source_pin,
                    model=inputs.model,
                    mcp_tools=tuple(sorted(catalogue)),
                )
            if frozenset(inputs.mcp_tools) != catalogue:
                raise ValueError("MCP representation must preserve the exact source catalogue")
            # A user's own connectors can't come along: each is a named loss, and
            # the Daimon MCP server is represented without them.
            connectors = frozenset(
                server.name
                for server in spec.mcp_servers or ()
                if server.name != DAIMON_SERVER_NAME
            )
            if any(_CONNECTOR_NAME.fullmatch(name) is None for name in connectors):
                # No named loss can spell it: represent nothing, so MCP drops whole.
                raise ValueError("unrepresentable connector name")
            connector_losses = tuple(f"mcp_server:{name}" for name in sorted(connectors))
            tools = tuple(
                tool
                for tool in spec.tools or ()
                if not (tool.kind == "mcp_toolset" and tool.name in connectors)
            )
            permissions = {name: "auto" for name in catalogue}
            for extension in spec.extensions or ():
                if extension.namespace == AGENT_TOOLS_NAMESPACE:
                    native = agent_tools(extension)
                    mcps = [
                        tool
                        for tool in native.tools
                        if isinstance(tool, MCPToolset) and tool.mcp_server_name not in connectors
                    ]
                    if len(mcps) != 1 or mcps[0].mcp_server_name != DAIMON_SERVER_NAME:
                        raise ValueError("unrepresented MCP toolset")
                    permissions = mcp_permissions(mcps[0], catalogue)
                    tools = (
                        *AgentToolsConfig(
                            tools=[
                                tool for tool in native.tools if not isinstance(tool, MCPToolset)
                            ]
                        ).intent(exact=True),
                        ToolSpec(name=DAIMON_SERVER_NAME, kind="mcp_toolset"),
                    )
            if not any(
                tool.kind == "mcp_toolset"
                and tool.name == DAIMON_SERVER_NAME
                and tool.permission == "auto"
                for tool in tools
            ):
                raise ValueError("source has no enabled daimon-mcp toolset")
            # No OpenAI/Gemini turn has a confirmation card, pinned or template:
            # every confirmation-gated or publishing tool stays unavailable (and
            # the server refuses it for their provider-turn credentials too),
            # except those whose card Daimon posts itself.
            from daimon.core.tool_safety import confirmation_gated_tools, self_carded_tools

            gated, carded = confirmation_gated_tools(), self_carded_tools()
            permissions = {
                # A self-carded tool runs (Daimon posts its card) unless the agent
                # turned it off, which stays a named loss as before.
                name: (
                    "auto"
                    if name in carded and permission != "disabled"
                    else "ask"
                    if name in gated
                    else permission
                )
                for name, permission in permissions.items()
            }
            if not any(permission == "auto" for permission in permissions.values()):
                raise ValueError("source has no automatically executable MCP tools")
            # URL fetching has no native hosted-Agents tool. Keep the exact
            # source capability visible as a consented loss instead of silently
            # treating shell HTTP execution as the provider's fetch tool.
            unavailable_builtin_tools = (
                ("builtin_tool:web_fetch",)
                if profile.provider == "openai"
                and any(tool.kind == "builtin" and tool.name == "web_fetch" for tool in tools)
                else ()
            )
            own_tools = tuple(tool for tool in tools if tool.kind != "mcp_toolset")
            template_losses: tuple[str, ...] = ()
            if template:
                # A template maps the source's execution tools to the provider's own
                # (OpenAI bash in its hosted environment, Gemini code_execution): both
                # are billed as tokens and container time already measured, never per
                # call. Web tools stay named losses: OpenAI's web_search is unpriced
                # and uncounted, and Gemini's grounding is off. Custom skills are a
                # whole loss its consent must name.
                web = tuple(
                    tool for tool in own_tools if tool.kind == "builtin" and tool.name in _WEB_TOOLS
                )
                unavailable_builtin_tools = tuple(
                    f"builtin_tool:{tool.name}" for tool in sorted(web, key=lambda t: t.name)
                )
                try:
                    mapped_tools = (
                        represented_execution_tools(
                            profile, tuple(tool for tool in own_tools if tool not in web)
                        )
                        if own_tools
                        else ()
                    )
                except InvalidConfig:
                    # Tools it can't map (custom tools, a partial toolset) go whole.
                    mapped_tools, unavailable_builtin_tools = (), ()
                    template_losses = ("tools",)
                if any(s.source in (None, "custom") for s in spec.skills or ()):
                    template_losses = ("skills", *template_losses)
            else:
                mapped_tools = represented_execution_tools(
                    profile,
                    tuple(
                        tool
                        for tool in own_tools
                        if not (
                            unavailable_builtin_tools
                            and tool.kind == "builtin"
                            and tool.name == "web_fetch"
                        )
                    ),
                )
            if any(
                tool.kind == "mcp_toolset"
                and (tool.name != DAIMON_SERVER_NAME or tool.permission != "auto")
                for tool in tools
            ):
                raise ValueError("unrepresented MCP toolset")
            reference = f"caller-mcp:{source.tenant_id}:{scope.account_id}:{source.agent_id}"
            servers = tuple(s for s in spec.mcp_servers or () if s.name not in connectors)
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
            # Hosted Agents MCP currently has no approval request/response
            # protocol (unlike Responses). Keep confirmation-gated tools out
            # of its executable allowlist and require explicit per-tool loss
            # consent. Never convert `ask` into `auto` or drop sibling tools.
            unavailable_mcp_tools = tuple(
                f"mcp_tool:{DAIMON_SERVER_NAME}:{name}"
                for name, permission in sorted(permissions.items())
                if permission == "ask"
            )
            connection = servers[0].model_copy(
                update={
                    "credential_ref": reference,
                    "tool_policy": {
                        "allowed_tools": [
                            name for name in inputs.mcp_tools if permissions[name] == "auto"
                        ]
                    },
                }
            )
            # The provider's built-in skills (docx, pptx, ...) have no bytes to
            # carry and run only there: another backend goes without them.
            portable = tuple(
                s for s in spec.skills or () if s.source in (None, "custom") and not template
            )
            if {item.source.id for item in inputs.skills} != {s.id for s in portable}:
                raise ValueError("skill representation is incomplete")
            unresolved = tuple(s for s in portable if s.version in (None, "latest"))
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
                    unavailable_mcp_tools=unavailable_mcp_tools,
                    unavailable_builtin_tools=unavailable_builtin_tools,
                    environment=inputs.environment,
                    unavailable_capabilities=(*template_losses, *connector_losses),
                )
            )
        if len({item.target.model.model_dump_json() for item in represented}) != len(represented):
            raise ValueError("ambiguous source representation")
        return source.model_copy(update={"representations": tuple(represented)})
    except (OSError, ValueError, DaimonError, MuxError, AnthropicError, SQLAlchemyError) as error:
        # Representations are optional capture evidence, never a new failure
        # mode for native callers or worker ceilings. Full deployments require
        # this evidence independently and refuse before any provider exposure.
        # Said once here, though: without it the agent silently loses its MCP
        # on the new provider (J2's Argon preview). Only our own fixed
        # sentences are logged, never a provider's or a validator's text.
        from daimon.core.channel_backend import backend_refusal_causes

        log.warning(
            "source_capture.representation_skipped",
            error=type(error).__name__,
            reason=_own_sentence(error),
            # Fixed labels (class, our frame, SQLSTATE) for the whole chain.
            causes=backend_refusal_causes(error),
            tenant_id=str(source.tenant_id),
            agent_id=str(source.agent_id),
            profile=revision.profile if revision is not None else None,
            model=revision.model if revision is not None else None,
            capability_preview=capability_preview,
        )
        return source


def _own_sentence(error: BaseException) -> str | None:
    """The fixed sentence of a ValueError raised by the capture itself, else None.

    Anything raised deeper (a catalogue read, a validator) may carry content.
    """
    frame = error.__traceback__
    while frame is not None and frame.tb_next is not None:
        frame = frame.tb_next
    if type(error) is not ValueError or frame is None:
        return None
    # The capture's own sentences, and the MCP catalogue reader's (J6: the live
    # catalogue read is the step that fails only in a deployment), are fixed
    # strings; nothing deeper is trusted.
    if frame.tb_frame.f_code is _capture_source_capabilities.__code__ or (
        frame.tb_frame.f_globals.get("__name__") == "daimon.core.mcp_oauth.discovery"
    ):
        return str(error)
    return None


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
        from mux.drivers.anthropic import AgentModelConfig

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


def _caller_private(binding: ResourceBinding) -> bool:
    """A login that belongs to the person asking (their caller-mcp vault, in any of
    its variants), never to the agent or a worker."""
    return any(
        value is not None and value.startswith(CALLER_VAULT_PREFIX)
        for value in (binding.id, binding.credential_ref)
    )


def worker_source_ceiling(source: BackendAgent) -> BackendAgent:
    """Exclude validated native-only capabilities and their paired resources.

    Nothing with its own login enters a worker ceiling: the agent can still run a
    fleet, its workers run without it, and no credential ever reaches a worker. Each
    loss is declared (`mcp`, `files`, `repository`, `memory`):
    - a connector with its own login (an MCP server carrying a credential, or one a
      projected credential pairs with), with its toolset and binding;
    - a stored connector login the projector can't pair with one server
      (`agent-mcp:<id>`): fail safe, workers then run without any external connector;
    - stored secrets the session would write to a file (`.env`);
    - repositories cloned with a login;
    - the caller's own logins (every `caller-mcp:` vault variant), never carried, and
      their private carry (`caller-resources`), declared as `files`;
    - a GitHub App session's repository vault (`app-session-vault:*`), as `repository`;
    - memory, while the agent's memory store doesn't exist yet (its first turn).
    """
    spec = source.effective_spec
    if spec is None:
        return source
    normalized, excluded = worker_spec_ceiling(spec)
    logins = login_mcp_servers(source)
    stored_logins = any(
        _stored_connector_login(binding) for binding in source.resource_requirements
    )
    if stored_logins:
        logins |= {server.name for server in spec.mcp_servers or ()}
    drops: set[str] = set()
    if logins - excluded:
        drops.add("mcp")
        normalized = normalized.model_copy(
            update={
                "mcp_servers": tuple(
                    server for server in normalized.mcp_servers or () if server.name not in logins
                ),
                "tools": tuple(
                    tool
                    for tool in normalized.tools or ()
                    if not (tool.kind == "mcp_toolset" and tool.name in logins)
                )
                if normalized.tools is not None
                else None,
            }
        )
    excluded |= logins
    credentials = {
        server.credential_ref
        for server in spec.mcp_servers or ()
        if server.name in logins and server.credential_ref is not None
    }
    resources: list[ResourceBinding] = []
    for binding in source.resource_requirements:
        paired = _mcp_credential_servers(source, binding)
        if (paired and paired <= excluded) or (
            binding.kind == "credential" and binding.credential_ref in credentials
        ):
            continue
        if _stored_connector_login(binding):
            continue
        if binding.kind == "credential" and binding.target_path is not None:
            drops.add("files")
            continue
        if _caller_private(binding):
            # The caller's own login (every caller-mcp vault variant, Slack turn and
            # hold included) is never worker authority: excluded, so nothing carries it.
            continue
        if binding.kind == "native" and binding.id == CALLER_RESOURCES_ID:
            # The caller's private carry (a DM, a seal, attached material): workers run
            # without it, declared. Not as `credentials`: that would discharge the
            # check for a login still among the resources.
            drops.add("files")
            continue
        if binding.kind == "native" and binding.id.startswith(APP_SESSION_VAULT_PREFIX):
            # A GitHub App session's repository vault: workers run without it, declared.
            drops.add("repository")
            continue
        resources.append(binding)
    repositories = tuple(repo for repo in source.repository_sources if repo.credential_ref is None)
    if len(repositories) != len(source.repository_sources):
        drops.add("repository")
    if source.memory_store_id is None and source.memory_required is not False:
        # The agent's memory store is created by its first session, so on its very
        # first turn there is none to mount: its workers run without memory.
        drops.add("memory")
    if drops:
        normalized = normalized.model_copy(
            update={"declared_drops": tuple(sorted({*normalized.declared_drops, *drops}))}
        )
    return source.model_copy(
        update={
            "effective_spec": normalized,
            "resource_requirements": tuple(resources),
            "repository_sources": repositories,
        }
    )


def _stored_connector_login(binding: ResourceBinding) -> bool:
    """A projected stored MCP login (`agent-mcp:<id>`), which names no server."""
    return (
        binding.kind == "credential"
        and binding.credential_ref is not None
        and binding.id == binding.credential_ref
        and binding.credential_ref.startswith("agent-mcp:")
        and binding.native is None
        and binding.resource is None
        and binding.target_path is None
    )


def login_mcp_servers(source: BackendAgent) -> frozenset[str]:
    """The agent's connectors that carry their own paired login; workers run without them."""
    spec = source.effective_spec
    if spec is None:
        return frozenset()
    names = {server.name for server in spec.mcp_servers or () if server.credential_ref}
    for binding in source.resource_requirements:
        names |= _mcp_credential_servers(source, binding)
    return frozenset(names)


def worker_spec_ceiling(spec: AgentSpec) -> tuple[AgentSpec, frozenset[str]]:
    """Normalize native toolsets without changing unrelated field presence."""
    from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE, agent_tools

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
    if (target.model.provider, target.model.id) == (ceiling.model.provider, ceiling.model.id):
        retained = target.model_copy(update={"extensions": controls})
        if _source_extension_obligation(extension, native_target=retained) is not None:
            raise ValueError("native worker model does not represent the granted controls")
        return retained
    # Another model, even of the same provider, may not accept the root's controls
    # (e.g. its effort level); the worker runs at its model's defaults, declared.
    if "model_controls" not in target.declared_drops:
        raise ValueError("native model controls require an explicit declared loss")
    return target.model_copy(update={"extensions": None})


def memory_mount_retained(source: BackendAgent, resources: tuple[ResourceBinding, ...]) -> bool:
    """Whether `resources` carry the agent's memory store exactly as its sessions mount
    it: the store is the only resource, in the agent's own resource scope, with a
    settled read/write posture. Only then can that store go where the resources go."""
    from daimon.core.memory_resource import memory_mount

    if len(resources) != 1 or source.memory_store_id is None:
        return False
    native, ref = resources[0].native, resources[0].resource
    return (
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
            {"resource": memory_mount(source.memory_store_id, read_only=source.memory_read_only)},
        )
    )


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
    ceiling_drops: set[str] = set()
    if worker_ceiling:
        before: set[str] = (
            set(source.effective_spec.declared_drops) if source.effective_spec else set()
        )
        try:
            source = worker_source_ceiling(source)
        except ValueError:
            return frozenset({"source_definition", "tools"})
        if source.effective_spec is not None:
            # What the ceiling left out (logins, stored secrets, logged-in repos) is
            # only discharged when the target declares the same loss.
            ceiling_drops = set(source.effective_spec.declared_drops) - before
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
    missing: set[str] = ceiling_drops - set(drops)
    if represented is not None:
        missing.update(set(represented.unavailable_mcp_tools) - set(drops))
        missing.update(set(represented.unavailable_builtin_tools) - set(drops))
        missing.update(set(represented.unavailable_capabilities) - set(drops))
    if not source_instructions_preserved(spec.system, target.system if target else None):
        missing.add("instructions")
    source_tools = tuple(spec.tools or ())
    from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE, agent_tools

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
            # A fleet worker on another model may declare the loss of the root's
            # controls exactly as a foreign-provider worker does.
            foreign_target = foreign_target or (
                worker_ceiling and target is not None and target.model.id != spec.model.id
            )
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
    # Connectors the representation names as lost go with their credentials.
    lost_servers: set[str] = (
        {
            item.removeprefix("mcp_server:")
            for item in represented.unavailable_capabilities
            if item.startswith("mcp_server:")
        }
        if represented is not None
        else set()
    )
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
            if server.name in lost_servers:
                continue
            if server.name in retained_mcp or server.credential_ref in retained_credentials:
                missing.add("credentials")
            elif "mcp" not in drops:
                missing.add("mcp")
    if (
        not native_retained
        and "credentials" not in drops
        and any(repository.credential_ref for repository in source.repository_sources)
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
            if paired <= lost_servers:
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
        # A declared `credentials` drop discharges a bare credential: the target runs
        # without it, and nothing carries it there.
        if resource.kind != "credential":
            missing.add("source_definition")
        elif "credentials" not in drops:
            missing.add("credentials")
    if (
        (source.memory_required is not False or source.memory_store_id is not None)
        and "memory" not in drops
        and not native_retained
        and not memory_mount_retained(source, resources)
    ):
        missing.add("memory")
    if source.environment is not None and source.environment.spec.native_config is not None:
        from mux.drivers.anthropic import EnvironmentConfig

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


#: The template drop a provider turn's consent named before Daimon posted the
#: switch card itself (`tool_safety.self_carded_tools`).
_SWITCH_TOOL_DROP: Final[str] = f"mcp_tool:{DAIMON_SERVER_NAME}:request_agent_backend_switch"


def _consented(target: AgentSpec | None, consent: _BackendDropConsent) -> bool:
    return (
        target is not None
        and target.declared_drops == consent.declared_drops
        and request_digest(target.model_dump(mode="json")) == consent.target_digest
    )


def _with_switch_tool_dropped(target: AgentSpec, source_system: str | None) -> AgentSpec | None:
    """`target` as a consent recorded before the switch tool was offered names it.

    That consent declared the tool a loss: not allowed on Daimon's MCP, one more
    drop, and the drops line in the instructions. Only that one difference is
    rebuilt; anything else still differs and still needs a fresh confirmation.
    """
    if _SWITCH_TOOL_DROP in target.declared_drops:
        return None
    tool = _SWITCH_TOOL_DROP.rsplit(":", 1)[1]
    from mux.contracts.resources import MCPConnection

    servers: list[MCPConnection] = []
    found = False
    for server in target.mcp_servers or ():
        allowed = server.tool_policy.get("allowed_tools") if server.tool_policy else None
        if server.name == DAIMON_SERVER_NAME and isinstance(allowed, list) and tool in allowed:
            found = True
            server = server.model_copy(
                update={
                    "tool_policy": {
                        **server.tool_policy,
                        "allowed_tools": [name for name in allowed if name != tool],
                    }
                }
            )
        servers.append(server)
    if not found:
        return None
    drops = tuple(sorted((*target.declared_drops, _SWITCH_TOOL_DROP)))
    return target.model_copy(
        update={
            "mcp_servers": tuple(servers),
            "declared_drops": drops,
            "system": compose_source_instructions(
                source_system, "Unavailable capabilities: " + ", ".join(drops) + "."
            ),
        }
    )


def _with_tools_dropped(target: AgentSpec, source_system: str | None) -> AgentSpec | None:
    """`target` as a consent recorded before a template kept the source's tools names it.

    That consent (before #1483) declared `tools` a whole loss: no tools on the
    target, `tools` among its drops, and neither of today's granular web losses
    (an OpenAI consent already named web fetch). Only that difference is rebuilt;
    the copy then runs with its tools from now on.
    """
    if "tools" in target.declared_drops or not target.tools:
        return None
    from mux.profiles.openai import PERSISTENT_WORKSPACE

    granular: set[str] = {"builtin_tool:web_search"}
    if target.model.provider != PERSISTENT_WORKSPACE.provider:
        granular.add("builtin_tool:web_fetch")
    drops = tuple(sorted({*target.declared_drops, "tools"} - granular))
    return target.model_copy(
        update={
            "tools": (),
            "declared_drops": drops,
            "system": compose_source_instructions(
                source_system, "Unavailable capabilities: " + ", ".join(drops) + "."
            ),
        }
    )


def _consented_before(
    target: AgentSpec, source_system: str | None, consent: _BackendDropConsent
) -> bool:
    """A consent recorded before a template kept the source's tools (#1483) or
    offered the switch tool (#1438).

    Either difference alone, or both, is rebuilt; anything else still needs a
    fresh confirmation.
    """
    without_tools = _with_tools_dropped(target, source_system)
    candidates = (
        _with_switch_tool_dropped(target, source_system),
        without_tools,
        _with_switch_tool_dropped(without_tools, source_system) if without_tools else None,
    )
    return any(_consented(candidate, consent) for candidate in candidates)


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
        if not _consented(target, consent) and not _consented_before(target, spec.system, consent):
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
        from daimon.core.turn.runtimes import ScopedTurnRuntimes, prepare_channel_runtime

        # Provider setup this turn's runtime needs first (e.g. the tenant's GPT
        # environment, created once). Only a real turn's preparation reaches here.
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        selected = await asyncio.wait_for(prepare_channel_runtime(selected), timeout=timeout)
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
        elif worker is None and runtime.materialize_root is not None:
            timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
            allocated = await asyncio.wait_for(runtime.materialize_root(selected), timeout=timeout)
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
    except AdmissionDenied as error:
        # A refusal that names its own cause (e.g. GPT still being set up for this
        # tenant) reaches the person as itself; anything else stays "unsupported".
        if error.reason != "backend_unsupported":
            raise
        raise AdmissionDenied(reason="backend_unsupported") from error
    except AgentBackendRefused as error:
        # A stale consent is not missing provider setup: name the fresh preview
        # the person needs. Any other backend refusal stays unsupported (J6g).
        if error.reason == "re_confirm":
            raise AdmissionDenied(reason="backend_unsupported", cause="re_confirm") from error
        raise AdmissionDenied(reason="backend_unsupported") from error
    except UnsupportedCapability as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    except Exception as error:
        from daimon.core.channel_backend import backend_refusal_causes, transient_failure

        # Anything else (a timeout included) is not proven missing setup. Name it
        # with fixed labels only (class, our frame, SQLSTATE; never its text) so
        # its real cause is never lost (smoke dce44708 J2: four inner causes logged
        # as "redacted"), and say busy when a retry can clear it.
        busy = transient_failure(error)
        log.warning(
            "turn.preparation_counterpart_error",
            causes=backend_refusal_causes(error),
            transient=busy,
            profile=(
                request.admission.backend_revision.profile
                if request.admission.backend_revision is not None
                else None
            ),
            worker=request.worker is not None,
        )
        raise AdmissionDenied(
            reason="backend_unsupported",
            cause="preparation_busy" if busy else "preparation_failed",
        ) from error


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
    @property
    def root_materialization(self) -> bool: ...
    @classmethod
    def model_validate(cls, obj: object) -> Self: ...


def template_source(data: object) -> object:
    """Map a worker template's `"*"` source pins to "no pin"; refuse them on any other row."""
    if not isinstance(data, dict):
        return data
    row = cast(dict[str, object], data)
    fields = [name for name in ("source_agent_id", "source_agent_version") if row.get(name) == "*"]
    if not fields:
        return row
    if row.get("worker_materialization") is not True:
        raise ValueError("only a worker template row may name any source agent")
    return {**row, **dict.fromkeys(fields)}


def validate_counterpart_pins(
    plan: CounterpartPlan, *, worker_materialization: bool = False
) -> None:
    """Legacy rows have no counterpart; partial source/catalog pins never authorize one."""
    if plan.root_materialization:
        # A tenant-wide agent template: identity comes from the admitted agent,
        # never from the row, so the row may pin none of it.
        if (
            worker_materialization
            or plan.source_agent_id is not None
            or plan.source_agent_version is not None
            or plan.counterpart_catalog_id is not None
            or plan.counterpart_catalog_revision is not None
            or plan.agent_id is not None
        ):
            raise ValueError("root materialization takes its identity from the admitted agent")
        return
    if worker_materialization:
        # A worker template names no source (both pins absent): it serves any
        # granted agent of its tenant, at that agent's live version.
        if (
            (plan.source_agent_id is None) != (plan.source_agent_version is None)
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


def _matching_rows(
    rows: list[dict[str, JsonValue]],
    revision: ConfigRevision,
    request: ProviderPreparationRequest,
    *,
    allow_wildcard: bool,
) -> list[dict[str, JsonValue]]:
    """`profile` and `tenant_id` are always exact. The other scope fields are
    exact-matched first; `allow_wildcard` additionally accepts `_WILDCARD` rows
    on those fields only — the caller tries exact-only before falling back."""
    from daimon.core.ma_identity import derive_agent_uuid

    if request.admission.backend_revision_origin != "agent":
        kind: TurnKind = "channel"
        source_agent_id, source_agent_version = None, None
    else:
        agent = request.admission.agent
        kind = "worker" if request.worker is not None else "root"
        source_agent_id = str(derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=agent.id))
        source_agent_version = agent.version
    return _rows_for(
        rows,
        revision,
        tenant_id=request.scope.tenant_id,
        account_id=request.scope.account_id,
        allow_wildcard=allow_wildcard,
        kind=kind,
        source_agent_id=source_agent_id,
        source_agent_version=source_agent_version,
    )


type TurnKind = Literal["channel", "root", "worker"]


def _same_agent(pinned: object, actual: str | None) -> bool:
    """Source pins are UUIDs: compare them as UUIDs, not by spelling."""
    try:
        return actual is not None and UUID(str(pinned)) == UUID(actual)
    except ValueError:
        return False


def _pinned_source(row: dict[str, JsonValue]) -> bool:
    return row.get("source_agent_id") not in (None, _WILDCARD)


def _rows_for(
    rows: list[dict[str, JsonValue]],
    revision: ConfigRevision,
    *,
    tenant_id: str,
    account_id: str,
    allow_wildcard: bool,
    kind: TurnKind,
    source_agent_id: str | None,
    source_agent_version: int | None,
) -> list[dict[str, JsonValue]]:

    def field_matches(row: dict[str, JsonValue], field: str, actual: object) -> bool:
        value = row.get(field)
        if value == actual:
            return True
        return allow_wildcard and value == _WILDCARD

    def origin_eligible(row: dict[str, JsonValue]) -> bool:
        # A channel's turn runs a fixed plan with no source. An agent's root turn
        # runs a root template or a row pinned to its source; a worker runs a
        # worker row or a fixed counterpart pinned to its source. Root and worker
        # templates never compete, so both coexist in one tenant.
        root = row.get("root_materialization") is True
        worker = row.get("worker_materialization") is True
        pinned = _pinned_source(row)
        if kind == "channel":
            return not (root or worker or pinned)
        if pinned and (
            not _same_agent(row.get("source_agent_id"), source_agent_id)
            or (
                source_agent_version is not None
                and row.get("source_agent_version") != source_agent_version
            )
        ):
            # Another agent's (or version's) pinned row: fall through to the template.
            return False
        if kind == "root":
            return root or (pinned and not worker)
        return worker or (pinned and not root)

    return [
        row
        for row in rows
        if origin_eligible(row)
        and row.get("profile") == revision.profile
        and row.get("tenant_id") == tenant_id
        and field_matches(row, "platform", revision.channel.platform)
        and field_matches(row, "channel_id", revision.channel.channel_id)
        and field_matches(row, "account_id", account_id)
        and manifest_configuration_matches(row, revision)
    ]


def manifest_hold_usd(
    revision: ConfigRevision,
    *,
    tenant_id: str,
    account_id: str,
    source_agent_id: str | None = None,
    source_agent_version: int | None = None,
) -> Decimal | None:
    """The per-turn hold of the row `select_runtime_manifest` would select, if any.

    Exact rows first, then the most specific wildcard row, as selection does: a
    channel's own row wins over a tenant-wide "*" row. None when no manifest is
    installed or readable, no single row is selected, or it names no valid hold.
    """
    from daimon.core.turn.runtime_defaults import default_rows

    try:
        rows = _configured_rows()
    except AdmissionDenied:
        return None
    try:
        # The rows a fleet worker's selection would see; without its source agent,
        # only rows that pin none.
        def worker_rows(
            allow_wildcard: bool, rows: list[dict[str, JsonValue]] = rows
        ) -> list[dict[str, JsonValue]]:
            return _rows_for(
                rows,
                revision,
                tenant_id=tenant_id,
                account_id=account_id,
                allow_wildcard=allow_wildcard,
                kind="worker",
                source_agent_id=source_agent_id,
                source_agent_version=source_agent_version,
            )

        selected = (
            worker_rows(False)
            or _most_specific(worker_rows(True))
            # Selection's code-default fallback; the hold agrees with the row it picks.
            or (
                []
                if _operator_configured(rows, tenant_id, revision)
                else _most_specific(worker_rows(True, default_rows(tenant_id)))
            )
        )
        if len(selected) != 1:
            return None
        hold = Decimal(str(selected[0]["hold_usd"]))
    except (OSError, ValidationError, KeyError, ArithmeticError, ValueError):
        return None
    return hold if hold.is_finite() and hold > 0 else None


def _most_specific(rows: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """The one matching row strictly narrower than every other, else all of them.

    A row is narrower when the scope fields it names exactly are a strict
    superset of another's: a channel's own row with `"*"` accounts beats a
    deployment-wide default. Rows naming different fields (a channel's row
    and an account's row) are incomparable and all remain, so they refuse
    as ambiguous instead of one silently shadowing the other's credentials.
    """

    def exact(row: dict[str, JsonValue]) -> frozenset[str]:
        return frozenset(field for field in _WILDCARDABLE_FIELDS if row.get(field) != _WILDCARD)

    for candidate in rows:
        if all(exact(other) < exact(candidate) for other in rows if other is not candidate):
            return [candidate]
    return rows


def _operator_configured(
    rows: list[dict[str, JsonValue]], tenant_id: str, revision: ConfigRevision
) -> bool:
    """Whether the operator's manifest has any row for this tenant's profile."""
    return any(
        row.get("tenant_id") == tenant_id and row.get("profile") == revision.profile for row in rows
    )


def _configured_rows() -> list[dict[str, JsonValue]]:
    """The operator manifest's rows; none when no manifest is configured.

    A configured manifest that can't be read refuses (`AdmissionDenied`) rather than
    falling back to the code defaults, so a broken operator file never widens access.
    """
    manifest_path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not manifest_path:
        return []
    try:
        with Path(manifest_path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise AdmissionDenied(reason="backend_unsupported")
        return _ROWS.validate_json(raw)
    except (OSError, ValidationError):
        raise AdmissionDenied(reason="backend_unsupported") from None


def select_runtime_manifest[Plan: CounterpartPlan](
    request: ProviderPreparationRequest, plan_type: type[Plan]
) -> Plan:
    """Select exactly one installed scope/configuration grant before credential discovery.

    A deployment-wide default is one row with `_WILDCARD` ("*") on `platform`,
    `channel_id` and/or `account_id` — never on `tenant_id` or `profile`. An
    exact match always wins over a wildcard row (computed first, tried first);
    wildcard rows are only consulted when no exact row matches. Among those,
    a row whose exactly named scope fields strictly include every other
    candidate's wins (a channel's own row, even with `"*"` accounts, beats a
    deployment-wide default); otherwise the existing "exactly one candidate
    or refuse" rule refuses them as ambiguous.
    The resolved plan always carries the request's real platform/channel_id/
    account_id — a wildcard row is a template, never a literal "*" identity.
    """

    from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
    from daimon.core.turn.runtime_defaults import default_rows

    revision = request.admission.backend_revision
    if revision is None:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        rows = _configured_rows()
        selected = _matching_rows(rows, revision, request, allow_wildcard=False)
        if not selected:
            selected = _most_specific(_matching_rows(rows, revision, request, allow_wildcard=True))
        if not selected and not _operator_configured(rows, request.scope.tenant_id, revision):
            # The operator configured nothing for this tenant's profile: the
            # deployment's code defaults serve it. Where the operator configured
            # the profile, its rows alone decide, so a narrower scope never widens.
            provider = get_profile(revision.profile).provider
            if provider in DEFAULT_KEY_ENV and not provider_key_present(provider):
                # The operator's step, named only in the log: set DEFAULT_KEY_ENV[provider].
                log.warning(
                    "provider_runtime.not_configured",
                    provider=provider,
                    key_env=DEFAULT_KEY_ENV[provider],
                )
                raise AdmissionDenied(
                    reason="backend_unsupported",
                    cause="provider_not_configured",
                    provider=provider,
                )
            defaults = default_rows(request.scope.tenant_id, request)
            selected = _most_specific(
                _matching_rows(defaults, revision, request, allow_wildcard=True)
            )
        if len(selected) != 1:
            raise AdmissionDenied(reason="backend_unsupported")
        resolved = dict(selected[0])
        for field, actual in (
            ("platform", revision.channel.platform),
            ("channel_id", revision.channel.channel_id),
            ("account_id", request.scope.account_id),
        ):
            if resolved.get(field) == _WILDCARD:
                resolved[field] = actual
        return plan_type.model_validate(resolved)
    except (OSError, ValidationError):
        raise AdmissionDenied(reason="backend_unsupported") from None


def manifest_has_row(request: ProviderPreparationRequest) -> bool:
    """Whether the operator's runtime file has any row for this tenant's profile.

    The same rule selection uses before it consults the code defaults
    (`_operator_configured`): where the operator configured the profile at all,
    its rows alone decide, so nothing is provisioned for a turn the defaults will
    never serve. This reads only the file, never the code defaults. A configured
    file that can't be read counts as having a row: never provision around a
    configuration the operator meant to install.
    """
    revision = request.admission.backend_revision
    if revision is None:
        return False
    try:
        rows = _configured_rows()
    except AdmissionDenied:
        return True
    return _operator_configured(rows, request.scope.tenant_id, revision)


def _worker_template(plan: CounterpartPlan) -> bool:
    """A worker row that names no source: identity comes from the request's admitted agent."""
    return plan.worker_materialization and plan.source_agent_id is None


def root_counterpart_catalog_id(request: ProviderPreparationRequest) -> str:
    """The one catalog entry a template row's counterpart lives at for this agent version.

    Keyed by the admitted agent's scoped identity, its MA version, the admitted
    profile and model and the consented target spec, so an agent edit, a model
    change or a different derivation gets a new entry, and an entry can never
    serve another agent, version, model or spec.
    """
    from daimon.core.ma_identity import derive_agent_uuid

    revision, target = request.admission.backend_revision, request.target_spec
    if revision is None or revision.model is None or target is None:
        raise AdmissionDenied(reason="backend_unsupported")
    agent_id = derive_agent_uuid(
        tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
    )
    # The consented target's digest: a different derived target never reuses an entry.
    spec = request_digest(target.model_dump(mode="json"))[:16]
    return (
        f"root:{agent_id}:{request.admission.agent.version}:{revision.profile}"
        f":{revision.model}:{spec}"
    )


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
    if plan.root_materialization:
        if (
            worker_materialization
            or request.worker is not None
            or request.admission.backend_revision_origin != "agent"
            or revision is None
            or not request.deps.channel_backends
            or not request.deps.agent_backends
            or request.deps.turn_path != "mux"
            or scope.tenant_id != str(request.tenant_id)
            or scope.account_id != str(request.admission.account_id)
            or scope.account_id != str(request.session_account_id)
            or (plan.tenant_id, plan.account_id) != (scope.tenant_id, scope.account_id)
            or (revision.channel.tenant_id, revision.channel.platform, revision.channel.channel_id)
            != (plan.tenant_id, plan.platform, plan.channel_id)
            or request.platform != plan.platform
            or revision.profile != plan.profile
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        if select_runtime_manifest(request, type(plan)) != plan:
            raise AdmissionDenied(reason="backend_unsupported")
        return revision
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
        or (
            not _worker_template(plan)
            and (
                plan.source_agent_id
                != derive_agent_uuid(
                    tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
                )
                or plan.source_agent_version != request.admission.agent.version
            )
        )
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


async def materialize_root_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    resources: NeutralResourceHost,
) -> AgentRevision:
    """Publish this agent version's counterpart from its consented target, once.

    Runs after source preparation proved the drop consent, so the target is the
    exact spec the admin confirmed. A concurrent first turn loses the catalog
    compare-and-set and reads the winner's entry; the native key is the catalog
    id, so native creation is idempotent too. `read_counterpart` then verifies
    the entry like any operator-installed counterpart.
    """
    from daimon.core.skills.ingest import SkillIngestError
    from daimon.core.stores.neutral_catalog import CatalogConflict
    from sqlalchemy import text

    validate_counterpart_request(plan, request)
    source, target = request.source, request.target_spec
    if (
        not plan.root_materialization
        or request.worker is not None
        or target is None
        or source is None
        or source.source is None
        or source.resource_scope != request.scope
        or resources.scope != request.scope
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    catalog_id = root_counterpart_catalog_id(request)
    try:
        return await resources.entry(catalog_id)
    except SkillIngestError:
        pass
    # Concurrent first turns publish one native agent: the loser waits, then reads
    # the winner's entry. Catalog entries (and so this key) are per caller account.
    body = json.dumps([request.scope.tenant_id, request.scope.account_id, plan.project, catalog_id])
    key = int.from_bytes(hashlib.sha256(body.encode()).digest()[:8], signed=True)
    async with request.deps.sessionmaker() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        try:
            return await resources.entry(catalog_id)
        except SkillIngestError:
            pass
        existing = await resources.catalog.get_agent(resources.scope, catalog_id)
        if existing is not None:
            # An archived entry is never republished over; this agent version stays refused.
            log.warning(
                "turn.root_counterpart_archived",
                catalog_id=catalog_id,
                lifecycle=existing.agent.lifecycle,
                tenant_id=request.scope.tenant_id,
            )
            raise AdmissionDenied(reason="backend_unsupported")
        try:
            return await resources.publish(catalog_id, target, expected_revision=0, key=catalog_id)
        except CatalogConflict:
            return await resources.entry(catalog_id)


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
    root = plan.root_materialization
    catalog_id = (
        acknowledged.catalog_id
        if acknowledged
        else root_counterpart_catalog_id(request)
        if root
        else cast(str, plan.counterpart_catalog_id)
    )
    from daimon.core.ma_identity import derive_agent_uuid

    # Templates (root or worker) bind to the request's own admitted source agent:
    # the agent this turn runs AS, which for a worker need not be the root.
    expected_source = (
        derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id)
        if root or _worker_template(plan)
        else plan.source_agent_id
    )
    if (
        source.tenant_id != request.tenant_id
        or source.agent_id != expected_source
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
                and not root
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
