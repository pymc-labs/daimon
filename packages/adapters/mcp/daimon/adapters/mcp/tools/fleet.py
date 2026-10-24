"""Fleet tools (on by default) over the authorized host orchestration service.

These tools accept resource handles, never authority. The host service resolves
current root membership, authorization, attenuation, reservations and operations;
it also owns result verification and the review barrier. Ordinary agent-chat
entry points retain their independent outside-chat restriction.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Protocol, cast

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.fleet_orchestration import FleetOrchestrator
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ModelRef, Provider, ResourceRef, Revision
from mux.contracts.resources import (
    AgentPatch,
    AgentSpec,
    CapabilityDrop,
    DeclaredDrop,
    SessionSpec,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue

if TYPE_CHECKING:
    from daimon.core.fleet_plan import Plan
    from daimon.core.neutral_resources import WorkerPermissions
    from daimon.core.stores.domain import TurnOriginRow
    from daimon.core.turn.deps import TurnDeps
    from mux.contracts.ids import Scope
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

PositiveMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
PositiveCount = Annotated[int, Field(gt=0, strict=True)]
OperationKey = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]


def _allowed_models(models: Sequence[ModelRef]) -> tuple[ModelRef, ...]:
    return tuple({(model.provider, model.id): model for model in models}.values())


def _allowed_line(models: Sequence[ModelRef]) -> str:
    from daimon.core.agent_model_names import model_name

    return ", ".join(f"{model_name(model)} ({model.id})" for model in models) or "none"


def _without_fast(models: Sequence[ModelRef]) -> tuple[ModelRef, ...]:
    from daimon.core.model_presets import OPUS_FAST

    return tuple(model for model in _allowed_models(models) if model.id != OPUS_FAST)


# J7m: asked for "Gemini Pro", the root started a paid worker on Argon unasked.
ASK_DONT_SUBSTITUTE = (
    "Ask the person which of these to use; don't start a worker on a model they didn't name."
)


def fleet_model(value: str, models: Sequence[ModelRef]) -> str:
    """The granted model id a worker model name means ("astra" -> gpt-6-astra), or a plain refusal.

    Only this run's allowed models are candidates, so a name never resolves to a
    model the fleet can't use; an ambiguous name lists what it could mean. The
    local Opus fast preset is never a worker model: only the legacy path expands it.
    """
    import re

    from daimon.core.agent_model_names import AmbiguousModel, resolve_model

    allowed = _without_fast(models)
    if "fast" in re.findall(r"[a-z]+", value.casefold()):
        # Refuse before resolving: close-match spelling would turn it into standard Opus.
        raise ToolError(
            f'"{value}" can\'t run as a fleet worker (the fast setting only applies to an '
            f"agent's own model); allowed: {_allowed_line(allowed)}. {ASK_DONT_SUBSTITUTE}"
        )
    try:
        return resolve_model(value, available=allowed).id
    except AmbiguousModel as error:
        raise ToolError(
            f"{error} Allowed in this fleet: {_allowed_line(allowed)}. {ASK_DONT_SUBSTITUTE}"
        ) from None
    except ValueError:
        raise ToolError(
            f'"{value}" isn\'t allowed in this fleet; allowed: {_allowed_line(allowed)}. '
            f"{ASK_DONT_SUBSTITUTE}"
        ) from None


#: Family words a person says alone ("fork yourself across haiku, luna and flash").
_FAMILIES = ("opus", "sonnet", "haiku", "flash", "luna", "astra", "argon")


def family_names(models: Sequence[ModelRef]) -> dict[str, JsonValue]:
    """Each family word that names an allowed model, mapped to the one it means (the
    newest in that family, as `fleet_model` resolves it)."""
    named: dict[str, JsonValue] = {}
    for word in _FAMILIES:
        try:
            named[word] = fleet_model(word, models)
        except ToolError:
            continue
    return named


_REASONS = {
    "not_subset": "is outside what this fleet allows",
    "unsupported": "isn't supported for this worker",
    "unbound": "is missing",
    "ambiguous": "is ambiguous",
    "stale": "is out of date; check the fleet again",
    "exhausted": "is used up",
}


async def _refusals_logged(
    operation_key: str, spawn: Callable[[], Awaitable[dict[str, JsonValue]]]
) -> dict[str, JsonValue]:
    """Log every refused spawn with a category; the root only ever gets plain text."""
    from daimon.core.fleet_orchestration import FleetRefused
    from daimon.core.neutral_resources import WorkerSpecRefused

    try:
        return await spawn()
    except ToolError as error:
        log.info(
            "fleet.spawn_refused",
            category="request",
            operation_key=operation_key,
            detail=str(error)[:300],
        )
        raise
    except WorkerSpecRefused as error:
        log.info(
            "fleet.spawn_refused",
            category="worker_spec",
            operation_key=operation_key,
            field=error.field,
            reason=error.reason,
        )
        what = (
            "model or model provider" if error.field == "backend" else error.field.replace("_", " ")
        )
        raise ToolError(
            f"The worker's {what} {_REASONS.get(error.reason, error.reason.replace('_', ' '))}."
        ) from None
    except FleetRefused as error:
        log.info(
            "fleet.spawn_refused",
            category="fleet",
            operation_key=operation_key,
            reason=error.reason,
        )
        raise ToolError(
            f"Fleet refused this worker: {error.reason.replace('_', ' ')}."
            + (f" {error.plain}" if error.plain else "")
        ) from None


#: How long a plan waits for the person before it expires (nothing ran; ask again).
_PLAN_TTL = timedelta(minutes=30)


class PlanWorker(BaseModel):
    """One worker the turn intends: a task on a model (its own if omitted), or an
    on-server agent by name."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=200)
    task: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    agent: str | None = Field(default=None, min_length=1, max_length=200)


class AgentWorker(BaseModel):
    """A worker that runs AS this on-server agent, with any losses it declares.

    `declared_drops` only takes capability away (whole capabilities such as
    "tools" or "skills"); the agent's own accepted losses apply anyway.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    declared_drops: tuple[CapabilityDrop, ...] = Field(default=(), max_length=9)


class FleetToolService(Protocol):
    """Authorized owner seam; implementations never trust handles as grants.

    Writes pass through the single worker resolver, current root authorization,
    atomic store/budget admission and the headless driver. Reads validate scoped
    journal evidence. Missing prerequisites refuse, including incomplete native
    counterpart preservation. A repeated operation returns its retained outcome.
    """

    async def spawn(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID | None,
        operation_key: str,
        role: str,
        backend: BackendConfig | None,
        agent: AgentSpec | AgentWorker | str | None,
        clone: ResourceRef | None,
        clone_revision: Revision | None,
        overrides: AgentPatch | None,
        session: SessionSpec | None,
        budget_usd: Decimal | None,
        deadline: datetime | None,
        max_turns: int | None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
        task: str | None = None,
        model: str | None = None,
        planned_workers: int | None = None,
        planned_models: Sequence[str] | None = None,
        plan_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]: ...

    async def plan(
        self,
        caller: AuthIdentity,
        *,
        workers: Sequence[PlanWorker],
        origin_context_id: uuid.UUID | None = None,
        result_contract: str | None = None,
    ) -> Mapping[str, JsonValue]: ...

    async def status(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]: ...

    async def kill(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID,
        worker_id: uuid.UUID | None,
        expected_version: int,
        operation_key: str,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]: ...

    async def resume(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID,
        worker_id: uuid.UUID | None,
        expected_version: int,
        operation_key: str,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]: ...

    async def collect(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]: ...

    async def agents(
        self, caller: AuthIdentity, *, origin_context_id: uuid.UUID | None = None
    ) -> Mapping[str, JsonValue]: ...


class FleetParentLoader(Protocol):
    """Host-authenticated current origin/binding projection, never name lookup.

    The loader checks the verified token against the actual live turn origin,
    binding, root grants and selected run. It must not pick a latest session or
    manufacture depth zero from an ordinary chat-agent ID. Missing projection
    is unavailable. Both platform and MCP callers use the shared core service.
    Scope principal_id retains the original run's authenticated orchestrator;
    the current interaction supplies Subject and cannot replace that principal.
    """

    async def __call__(
        self,
        caller: AuthIdentity,
        *,
        origin_context_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> tuple[WorkerPermissions, TenantAccessPolicy]: ...


class HostFleetToolService:
    """The MCP bridge; authority is freshly loaded for every tool invocation."""

    def __init__(
        self,
        orchestrator: FleetOrchestrator,
        parent_loader: FleetParentLoader | None = None,
        *,
        runtime: McpRuntime | None = None,
        deps: TurnDeps | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._parent_loader = parent_loader
        self._runtime = runtime
        self._deps = deps

    def _plan_sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._runtime is not None:
            return self._runtime.session_factory
        if self._deps is not None:
            return self._deps.sessionmaker
        raise ToolError("Fleet plans are unavailable: host prerequisites are not installed.")

    def _plan_required(self, caller: AuthIdentity) -> bool:
        """`fleet.require_plan`, for a chat turn (agent keys, the hub and routines keep
        explicit parameters within the caps)."""
        settings = self._runtime.settings if self._runtime is not None else None
        return (
            settings is not None
            and settings.fleet.require_plan
            and caller.chat_agent_id is not None
            and caller.agent_id is None
        )

    async def plan(
        self,
        caller: AuthIdentity,
        *,
        workers: Sequence[PlanWorker],
        origin_context_id: uuid.UUID | None = None,
        result_contract: str | None = None,
    ) -> Mapping[str, JsonValue]:
        """A pending fleet plan for this turn's thread, from the agent's fleet limits."""
        from daimon.core.fleet_plan import PlanCaps, PlannedWorker, PlanRefused, build_plan
        from daimon.core.neutral_resources import worker_runtime_ceiling
        from daimon.core.stores.fleet_plans import create_plan

        parent, _ = await self._parent(caller, origin_context_id=origin_context_id)
        allowed = _without_fast((*parent.models, parent.spec.model))
        planned: list[PlannedWorker] = []
        for worker in workers:
            if worker.agent is not None:
                planned.append(
                    PlannedWorker(role=worker.role, task=worker.task, agent=worker.agent)
                )
                continue
            model = (
                fleet_model(worker.model, allowed)
                if worker.model is not None
                else parent.spec.model.id
            )
            planned.append(PlannedWorker(role=worker.role, task=worker.task, model=model))
        now = datetime.now(UTC)
        providers: set[Provider] = {
            model.provider for model in allowed if model.id in {w.model for w in planned}
        } or {parent.spec.model.provider}
        ceilings = [
            ceiling
            for provider in providers
            if (ceiling := worker_runtime_ceiling(parent, provider)) is not None
        ]
        seconds = int((parent.limits.deadline - now).total_seconds())
        if ceilings:
            seconds = min(seconds, *ceilings)
        settings = self._runtime.settings.fleet if self._runtime is not None else None
        try:
            caps = PlanCaps(
                max_workers=settings.max_workers_per_run if settings is not None else 8,
                budget_usd=parent.limits.budget_usd,
                worker_seconds=max(seconds, 1),
                max_turns=parent.limits.max_turns,
                models=tuple(model.id for model in allowed),
            )
            plan = build_plan(caps, tuple(planned), result_contract=result_contract)
        except PlanRefused as exc:
            raise ToolError(f"{exc} Nothing was started.") from exc
        root = parent.fleet.root
        async with self._plan_sessions().begin() as session:
            plan_id = await create_plan(
                session,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                platform=root.channel.platform,
                channel_id=root.channel.channel_id,
                thread_id=root.thread_id,
                plan=plan,
                caps=caps,
                now=now,
                ttl=_PLAN_TTL,
            )
        return {
            "plan_id": str(plan_id),
            "plan": cast(JsonValue, plan.model_dump(mode="json")),
            "caps": cast(JsonValue, caps.model_dump(mode="json")),
            "status": "pending",
        }

    async def _accepted_plan_for(
        self, caller: AuthIdentity, parent: WorkerPermissions, plan_id: uuid.UUID | None
    ) -> Plan | None:
        """With `fleet.require_plan` on, a chat turn's spawn runs only under an accepted
        plan for its own thread; None when no plan is required."""
        from daimon.core.stores.fleet_plans import accepted_plan

        if not self._plan_required(caller):
            return None
        if plan_id is None:
            raise ToolError(
                "This fleet needs a plan the person accepts first: call fleet_plan, and start "
                "the workers once they accept it. Nothing was started."
            )
        root = parent.fleet.root
        async with self._plan_sessions()() as session:
            plan = await accepted_plan(
                session,
                plan_id=plan_id,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                platform=root.channel.platform,
                channel_id=root.channel.channel_id,
                thread_id=root.thread_id,
            )
        if plan is None:
            raise ToolError(
                "That fleet plan isn't accepted for this conversation (it is pending, "
                "expired, or another one's). Nothing was started."
            )
        return plan

    async def _agent_obstacle(
        self, caller: AuthIdentity, origin: TurnOriginRow, scope: Scope
    ) -> str | None:
        """Why the responding agent's own setup keeps it from a fleet, read in this process.

        Only for a turn that never retained a root: a root that ended or went stale is
        just unavailable. Any failure here is no answer, never an error.
        """
        if self._runtime is None or self._deps is None:
            return None
        from daimon.adapters.mcp.tools.fleet_config import fleet_root_obstacle_for
        from daimon.core.channel_backend import load_backend_agent
        from daimon.core.stores import mux_state

        try:
            async with self._deps.sessionmaker() as db:
                if await mux_state.get_fleet_root_operation(db, scope, origin_id=origin.id):
                    return None
            agent = await load_backend_agent(
                self._runtime.client,
                self._runtime.session_factory,
                tenant_id=caller.tenant_id,
                agent_name=origin.responder_name,
            )
        except Exception as error:
            log.warning("fleet.obstacle_lookup_failed", cause=type(error).__name__)
            return None
        if agent.agent_id != caller.chat_agent_id:
            return None  # a renamed or replaced namesake is not this turn's agent
        return await fleet_root_obstacle_for(self._runtime, caller, agent)

    async def _parent(
        self,
        caller: AuthIdentity,
        *,
        origin_context_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        drain: bool = False,
    ) -> tuple[WorkerPermissions, TenantAccessPolicy]:
        if self._parent_loader is None and self._deps is None:
            raise ToolError("Fleet orchestration is unavailable: current root is not bound.")
        origin = None
        if self._runtime is not None:
            from daimon.adapters.mcp.tools._channel_policy import get_verified_origin

            origin = await get_verified_origin(
                self._runtime, caller, str(origin_context_id) if origin_context_id else None
            )
            if origin is None or origin.is_setup:
                raise ToolError("Fleet orchestration refused: active_origin_required.")
        if self._parent_loader is None:
            from daimon.adapters.mcp.tools._authz_facts import mcp_subject
            from daimon.core.mux_backend import resource_scope
            from daimon.core.neutral_resources import WorkerSpecRefused
            from daimon.core.turn.runtime_manifest import read_frozen_root
            from mux.errors import ScopeViolation

            if origin is None or self._deps is None:
                raise ToolError("Fleet orchestration refused: active_origin_required.")
            scope = resource_scope(
                tenant_id=str(caller.tenant_id),
                account_id=str(caller.account_id),
                authorization_id="fleet-origin:" + str(origin.id),
            )
            try:
                parent, policy = await read_frozen_root(
                    self._deps,
                    scope,
                    origin_id=origin.id,
                    run_id=run_id,
                    now=datetime.now(UTC),
                    drain=drain,
                )
            except (WorkerSpecRefused, ScopeViolation):
                from daimon.core.neutral_resources import fleet_root_obstacle_text
                from daimon.core.turn.runtime_manifest import root_refusal

                # The turn ran with fleet off because the agent's own setup can't
                # become worker authority; say why instead of a closed code.
                obstacle = root_refusal(origin.id)
                if obstacle is not None:
                    raise ToolError(fleet_root_obstacle_text(obstacle)) from None
                from daimon.core.fleet_configuration import FLEET_UNAVAILABLE_HERE
                from daimon.core.turn.runtime_manifest import default_fleet_unavailable_here

                # No default fleet where results can't be delivered yet; a custom
                # grant's root that went stale is still just unavailable.
                if caller.chat_agent_id is not None and await default_fleet_unavailable_here(
                    self._deps,
                    tenant_id=caller.tenant_id,
                    agent_id=caller.chat_agent_id,
                    platform=caller.platform,
                    now=datetime.now(UTC),
                ):
                    raise ToolError(FLEET_UNAVAILABLE_HERE) from None
                # The turn that couldn't freeze its root ran in the chat adapter's
                # process; ask this agent's own setup here instead of a closed code.
                obstacle = await self._agent_obstacle(caller, origin, scope)
                if obstacle is not None:
                    raise ToolError(fleet_root_obstacle_text(obstacle)) from None
                raise ToolError("Fleet orchestration refused: root_unavailable.") from None
            parent = replace(parent, subject=mcp_subject(caller, is_admin=caller.is_admin))
        else:
            parent, policy = (
                await self._parent_loader(caller)
                if origin_context_id is None
                else await self._parent_loader(
                    caller, origin_context_id=origin_context_id, run_id=run_id
                )
            )
        facts = parent.fleet
        if origin is not None and (
            (self._parent_loader is not None and facts.origin_id != origin.id)
            or facts.root.channel.channel_id != origin.parent_channel_id
            or facts.root.thread_id != origin.thread_id
        ):
            raise ToolError("Fleet orchestration refused: caller_root_mismatch.")
        if (
            facts.scope.tenant_id != str(caller.tenant_id)
            or facts.scope.account_id != str(caller.account_id)
            or facts.account_id != caller.account_id
            or facts.root.channel.platform != caller.platform
            or parent.subject.platform_user_id != caller.platform_user_id
            or caller.chat_agent_id is None
            or parent.agent_id != caller.chat_agent_id
            or parent.subject.via_agent_key
            or (parent.subject.is_admin and not caller.is_admin)
            or caller.agent_id is not None
        ):
            raise ToolError("Fleet orchestration refused: caller_root_mismatch.")
        return parent, policy

    async def spawn(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID | None,
        operation_key: str,
        role: str,
        backend: BackendConfig | None,
        agent: AgentSpec | AgentWorker | str | None,
        clone: ResourceRef | None,
        clone_revision: Revision | None,
        overrides: AgentPatch | None,
        session: SessionSpec | None,
        budget_usd: Decimal | None,
        deadline: datetime | None,
        max_turns: int | None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
        task: str | None = None,
        model: str | None = None,
        planned_workers: int | None = None,
        planned_models: Sequence[str] | None = None,
        plan_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id
        )
        from daimon.core.neutral_resources import (
            planned_providers,
            task_worker_spec,
            worker_runtime_ceiling,
        )

        as_agent_drops: tuple[DeclaredDrop, ...] = ()
        if isinstance(agent, AgentWorker):
            agent, as_agent_drops = agent.name, agent.declared_drops
        as_agent = agent if isinstance(agent, str) else None
        spec = agent if not isinstance(agent, str) else None
        worker_parent: WorkerPermissions | None = None
        if as_agent is not None:
            # The worker runs AS that on-server agent; its own model sets the defaults.
            worker_parent = await self._orchestrator.agent_worker(
                parent, policy, agent_name=as_agent
            )
        elif spec is None and clone is None and task is not None:
            if model is not None:
                # A natural name ("astra", "opus 5.5") means the granted id it names.
                model = fleet_model(model, (*parent.models, parent.spec.model))
            spec = task_worker_spec(parent, role=role, task=task, model=model)
        providers: list[Provider] | None = None
        if planned_models is not None:
            plan_parent = parent
            if worker_parent is not None:
                # Agent workers run on their own agents' models, not the grant's list.
                models = [*parent.models, worker_parent.models[0]]
                for row in await self._orchestrator.worker_agents(parent, policy):
                    backend_id, model_id = row.get("backend"), row.get("model")
                    if isinstance(backend_id, str) and isinstance(model_id, str):
                        models.append(ModelRef(provider=cast(Provider, backend_id), id=model_id))
                plan_parent = replace(parent, models=tuple(models))
            # Names resolve to granted ids first, so "astra" no longer refuses the plan;
            # a name that resolves to nothing still refuses it, in plain words.
            planned_models = [
                fleet_model(name, (*plan_parent.models, plan_parent.spec.model))
                for name in planned_models
            ]
            granted = {model.id for model in (*plan_parent.models, plan_parent.spec.model)}
            # Safety net: fleet_model only returns allowed ids, so this should never fire.
            unknown = sorted(set(planned_models) - granted)
            if unknown:
                raise ToolError(
                    f"planned_models names a model the fleet grant does not allow: {unknown[0]}."
                    if worker_parent is None
                    else "planned_models names a model this fleet's agents can't use: "
                    f"{unknown[0]}."
                )
            if planned_workers is not None and planned_workers < len(planned_models):
                raise ToolError("planned_workers is smaller than the number of planned_models.")
            providers = planned_providers(plan_parent, planned_models)
            planned_workers = len(providers) if planned_workers is None else planned_workers
        provider = (
            backend.backend
            if backend is not None and backend.backend is not None
            else spec.model.provider
            if spec is not None
            else overrides.model.provider
            if overrides is not None and overrides.model is not None
            else worker_parent.models[0].provider
            if worker_parent is not None
            else parent.spec.model.provider
        )
        planned_on_provider = providers.count(provider) if providers is not None else None
        now = datetime.now(UTC)
        plan = await self._accepted_plan_for(caller, parent, plan_id)
        if plan is not None:
            from daimon.core.fleet_plan import PlanRefused, spawn_within_plan

            try:
                budget_usd, seconds, max_turns = spawn_within_plan(
                    plan,
                    model=spec.model.id if spec is not None else None,
                    agent=as_agent,
                    budget_usd=budget_usd,
                    worker_seconds=int((deadline - now).total_seconds())
                    if deadline is not None
                    else None,
                    max_turns=max_turns,
                )
            except PlanRefused as exc:
                raise ToolError(f"{exc} Nothing was started.") from exc
            deadline = now + timedelta(seconds=seconds)
        limits = await self._orchestrator.spawn_limits(
            parent,
            policy,
            operation_key=operation_key,
            provider=provider,
            now=now,
            budget_usd=budget_usd,
            deadline=deadline,
            max_turns=max_turns,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            planned_workers=planned_workers,
            planned_on_provider=planned_on_provider,
            max_runtime_s=worker_runtime_ceiling(parent, provider, backend),
        )
        return await self._orchestrator.spawn(
            parent,
            policy,
            now=now,
            run_id=run_id,
            operation_key=operation_key,
            role=role,
            backend=backend,
            limits=limits,
            fill_token_limits=max_input_tokens is None and max_output_tokens is None,
            as_agent=as_agent,
            as_agent_drops=as_agent_drops,
            task=task if as_agent is not None else None,
            agent=spec,
            clone=clone,
            clone_revision=clone_revision,
            overrides=overrides,
            session=session,
            result_contract=result_contract,
            reviewer_of=reviewer_of,
        )

    async def status(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id, drain=True
        )
        return await self._orchestrator.status(
            parent, policy, run_id=parent.fleet.run_id if run_id is None else run_id
        )

    async def kill(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID,
        worker_id: uuid.UUID | None,
        expected_version: int,
        operation_key: str,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id, drain=True
        )
        return await self._orchestrator.control(
            parent,
            policy,
            run_id=run_id,
            worker_id=worker_id,
            action="kill",
            expected_version=expected_version,
            operation_key=operation_key,
            now=datetime.now(UTC),
        )

    async def resume(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID,
        worker_id: uuid.UUID | None,
        expected_version: int,
        operation_key: str,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id
        )
        return await self._orchestrator.control(
            parent,
            policy,
            run_id=run_id,
            worker_id=worker_id,
            action="resume",
            expected_version=expected_version,
            operation_key=operation_key,
            now=datetime.now(UTC),
        )

    async def collect(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id, drain=True
        )
        return await self._orchestrator.collect(
            parent, policy, run_id=parent.fleet.run_id if run_id is None else run_id
        )

    async def agents(
        self, caller: AuthIdentity, *, origin_context_id: uuid.UUID | None = None
    ) -> Mapping[str, JsonValue]:
        from daimon.core.agent_model_names import model_name

        parent, policy = await self._parent(caller, origin_context_id=origin_context_id)
        models = _without_fast((*parent.models, parent.spec.model))
        return {
            "agents": list(await self._orchestrator.worker_agents(parent, policy)),
            # The models a task worker (a copy of you) may run on, by plain name.
            "models": [
                {"name": model_name(model), "id": model.id, "provider": model.provider}
                for model in models
            ],
            # What a family name alone means here: its newest allowed model, exactly as
            # fleet_spawn resolves it (J10b: "haiku" drew a question about 4.5 vs 5.5).
            "family_names": family_names(models),
        }


def register_fleet_tools(
    mcp: FastMCP,
    runtime: McpRuntime,
    *,
    enabled: bool = False,
    service: FleetToolService | None = None,
) -> None:
    """Register only for explicit fleet and neutral-turn configuration.

    An absent owner service leaves opted-in tools explicitly unavailable; it
    cannot fall back to self-only agent-chat tools or a provider SDK.
    """
    if not enabled:
        return
    turn = runtime.settings.turn
    if turn.path != "mux" or not turn.channel_backends or not turn.agent_backends:
        return

    def owner() -> FleetToolService:
        if service is None:
            raise ToolError(
                "Fleet orchestration is unavailable: host prerequisites are not installed."
            )
        return service

    async def spawn(
        ctx: Context,
        *,
        run_id: uuid.UUID | None,
        operation_key: str,
        role: str,
        backend: BackendConfig | None,
        agent: AgentSpec | AgentWorker | str | None,
        clone: ResourceRef | None,
        clone_revision: Revision | None,
        overrides: AgentPatch | None,
        session: SessionSpec | None,
        budget_usd: Decimal | None,
        deadline: datetime | None,
        max_turns: int | None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
        task: str | None = None,
        model: str | None = None,
        planned_workers: int | None = None,
        planned_models: list[str] | None = None,
        plan_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        async def checked() -> dict[str, JsonValue]:
            return await _spawn_checked(
                ctx,
                run_id=run_id,
                operation_key=operation_key,
                role=role,
                backend=backend,
                agent=agent,
                clone=clone,
                clone_revision=clone_revision,
                overrides=overrides,
                session=session,
                budget_usd=budget_usd,
                deadline=deadline,
                max_turns=max_turns,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                result_contract=result_contract,
                reviewer_of=reviewer_of,
                origin_context_id=origin_context_id,
                task=task,
                model=model,
                planned_workers=planned_workers,
                planned_models=planned_models,
                plan_id=plan_id,
            )

        return await _refusals_logged(operation_key, checked)

    async def _spawn_checked(
        ctx: Context,
        *,
        run_id: uuid.UUID | None,
        operation_key: str,
        role: str,
        backend: BackendConfig | None,
        agent: AgentSpec | AgentWorker | str | None,
        clone: ResourceRef | None,
        clone_revision: Revision | None,
        overrides: AgentPatch | None,
        session: SessionSpec | None,
        budget_usd: Decimal | None,
        deadline: datetime | None,
        max_turns: int | None,
        max_input_tokens: int | None,
        max_output_tokens: int | None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        origin_context_id: uuid.UUID | None,
        task: str | None,
        model: str | None,
        planned_workers: int | None,
        planned_models: list[str] | None,
        plan_id: uuid.UUID | None,
    ) -> dict[str, JsonValue]:
        if isinstance(agent, (str, AgentWorker)):
            name = agent if isinstance(agent, str) else agent.name
            if not name.strip() or clone is not None or model is not None:
                raise ToolError(
                    "A worker agent is named on its own: give its task, not a model or a clone."
                )
        elif sum(item is not None for item in (task, agent, clone)) != 1:
            raise ToolError("Give the worker a task, an agent spec or a pinned clone, exactly one.")
        if model is not None and task is None:
            raise ToolError("A model choice goes with a task.")
        if clone is None and (clone_revision is not None or overrides is not None):
            raise ToolError("Clone revisions and overrides require a clone.")
        if clone is not None and (clone.kind != "agent" or clone_revision is None):
            raise ToolError("A clone requires an agent resource and its current revision.")
        if deadline is not None and (deadline.tzinfo is None or deadline.utcoffset() is None):
            raise ToolError("A worker deadline requires a timezone.")
        caller = await _auth(ctx)
        return dict(
            await owner().spawn(
                caller,
                run_id=run_id,
                origin_context_id=origin_context_id,
                operation_key=operation_key,
                role=role,
                backend=backend,
                agent=agent,
                clone=clone,
                clone_revision=clone_revision,
                overrides=overrides,
                session=session,
                budget_usd=budget_usd,
                deadline=deadline,
                max_turns=max_turns,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                result_contract=result_contract,
                reviewer_of=reviewer_of,
                task=task,
                model=model,
                planned_workers=planned_workers,
                planned_models=planned_models,
                plan_id=plan_id,
            )
        )

    if runtime.settings.fleet.require_plan:

        @mcp.tool(tags={"fleet"})
        async def fleet_plan(  # pyright: ignore[reportUnusedFunction]
            ctx: Context,
            workers: Annotated[list[PlanWorker], Field(min_length=1)],
            origin_context_id: uuid.UUID | None = None,
            result_contract: Annotated[str, Field(max_length=2000)] | None = None,
        ) -> dict[str, JsonValue]:
            """Plan a fleet run before it starts; the person accepts or changes it.

            Call this FIRST for any fleet ("fork yourself across haiku, luna and
            flash"): one entry per worker, each with role, task and model (or agent
            for an on-server agent). It returns a pending plan with its plan_id and the
            recommended defaults (each worker's budget, time and turns, from this
            agent's fleet limits). Show the person the plan and let them accept or
            change it; nothing starts or is spent until they accept. Then pass the
            plan_id to every fleet_spawn of the run; a spawn without an accepted plan,
            or one past it, is refused. Pass origin_context_id from your turn controls.
            """
            return dict(
                await owner().plan(
                    await _auth(ctx),
                    workers=workers,
                    origin_context_id=origin_context_id,
                    result_contract=result_contract,
                )
            )

    @mcp.tool(tags={"fleet"})
    async def fleet_spawn(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        operation_key: OperationKey,
        role: str,
        task: str | None = None,
        model: str | None = None,
        planned_workers: PositiveCount | None = None,
        planned_models: Annotated[list[str], Field(min_length=1)] | None = None,
        plan_id: uuid.UUID | None = None,
        origin_context_id: uuid.UUID | None = None,
        backend: BackendConfig | None = None,
        budget_usd: PositiveMoney | None = None,
        deadline: datetime | None = None,
        max_turns: PositiveCount | None = None,
        max_input_tokens: PositiveCount | None = None,
        max_output_tokens: PositiveCount | None = None,
        run_id: uuid.UUID | None = None,
        agent: AgentSpec | AgentWorker | str | None = None,
        clone: ResourceRef | None = None,
        clone_revision: Revision | None = None,
        overrides: AgentPatch | None = None,
        session: SessionSpec | None = None,
        result_contract: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        """Start one worker in this turn's fleet run. Call once per worker, in parallel.

        This is what "fork yourself across Opus 5.5, Astra and Argon", "fork
        yourself onto gpt-6-luna", "run copies of yourself on X and Y" or "compare
        these models" means: start one task worker per named model with task + model.
        No admin is needed and no saved agent is made (fork_agent is only for a
        saved, named copy, which keeps its source's model). A model that isn't
        allowed here is refused with the allowed list; say that reason, never an
        admin/owner rule.
        Usually pass only operation_key (unique per worker, e.g. "W1"), role,
        task (the worker's full instructions), model (a model the fleet
        allows, by id or plain name, e.g. "gpt-6-astra" or "astra"; omit it
        for your own model) and planned_models (the model of every worker you
        are starting, e.g. ["opus 5.5", "astra", "argon"], the same list on
        every call), plus origin_context_id from your turn controls.
        fleet_agents lists the allowed models by name. A family name alone
        ("haiku") means its newest model (Claude Haiku 5.5): never ask which
        version, just name the model you started. A name that matches none, or
        several families, is refused with the allowed list.
        If the person names a model that isn't on that list (e.g. "Gemini
        Pro" when only Flash and Argon are allowed), don't start a worker:
        tell them it isn't available here, offer the closest allowed model,
        and ask which to use. Never run a paid worker on a substitute they
        didn't choose.
        Leave everything else out: the host binds this turn's run (never
        invent a run_id), picks the granted backend, splits the budget left
        evenly across your planned workers (and each provider's cap across
        the planned workers on that provider) and gives each worker up to the
        fleet's worker time, within the run's deadline (a later deadline is
        shortened to it).
        planned_workers (a plain count) is the older form of the same hint. A
        worker on another model than yours runs at that model's default
        effort. Budgets above the grant are refused. After spawning, end your
        turn; you are woken to fleet_collect.
        To run a worker AS one of this server's agents (its own configured
        model, tools and skills, under your permissions), pass agent=<its name>
        and its task, with no model; fleet_agents lists the agents you can use.
        To have it run without some of its capabilities, pass
        agent={"name": <its name>, "declared_drops": ["tools", ...]}. Its spawn
        reply lists every loss it runs with.
        Advanced: an agent spec or an authorized clone with sparse overrides
        instead of task. Retries reuse operation_key.
        """
        return await spawn(
            ctx,
            task=task,
            model=model,
            planned_workers=planned_workers,
            planned_models=planned_models,
            plan_id=plan_id,
            run_id=run_id,
            origin_context_id=origin_context_id,
            operation_key=operation_key,
            role=role,
            backend=backend,
            agent=agent,
            clone=clone,
            clone_revision=clone_revision,
            overrides=overrides,
            session=session,
            budget_usd=budget_usd,
            deadline=deadline,
            max_turns=max_turns,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            result_contract=result_contract,
            reviewer_of=None,
        )

    @mcp.tool(tags={"fleet"})
    async def fleet_status(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID | None = None,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Read the run's durable worker status; do not start work.

        Omit run_id for this turn's run.
        """
        return dict(
            await owner().status(
                await _auth(ctx), run_id=run_id, origin_context_id=origin_context_id
            )
        )

    @mcp.tool(tags={"fleet"})
    async def fleet_kill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID,
        operation_key: OperationKey,
        expected_version: PositiveCount,
        worker_id: uuid.UUID | None = None,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Request a scoped stop; acknowledgement is not proof the worker stopped."""
        return dict(
            await owner().kill(
                await _auth(ctx),
                run_id=run_id,
                origin_context_id=origin_context_id,
                worker_id=worker_id,
                operation_key=operation_key,
                expected_version=expected_version,
            )
        )

    @mcp.tool(tags={"fleet"})
    async def fleet_resume(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID,
        operation_key: OperationKey,
        expected_version: PositiveCount,
        worker_id: uuid.UUID | None = None,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Reconcile and explicitly resume under original ceilings and current authority."""
        return dict(
            await owner().resume(
                await _auth(ctx),
                run_id=run_id,
                origin_context_id=origin_context_id,
                worker_id=worker_id,
                operation_key=operation_key,
                expected_version=expected_version,
            )
        )

    @mcp.tool(tags={"fleet"})
    async def fleet_cross_review(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID,
        reviewed_worker_id: uuid.UUID,
        operation_key: OperationKey,
        backend: BackendConfig,
        budget_usd: PositiveMoney,
        deadline: datetime,
        role: str = "review",
        max_turns: PositiveCount = 1,
        max_input_tokens: PositiveCount | None = None,
        max_output_tokens: PositiveCount | None = None,
        agent: AgentSpec | None = None,
        clone: ResourceRef | None = None,
        clone_revision: Revision | None = None,
        overrides: AgentPatch | None = None,
        session: SessionSpec | None = None,
        result_contract: dict[str, JsonValue] | None = None,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Spawn a reviewer of verified journal evidence through the same worker path.

        The host resolves the reviewed result; caller text cannot replace it.
        An unavailable, failed or refused review blocks collection.
        """
        return await spawn(
            ctx,
            run_id=run_id,
            origin_context_id=origin_context_id,
            operation_key=operation_key,
            role=role,
            backend=backend,
            agent=agent,
            clone=clone,
            clone_revision=clone_revision,
            overrides=overrides,
            session=session,
            budget_usd=budget_usd,
            deadline=deadline,
            max_turns=max_turns,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            result_contract=result_contract,
            reviewer_of=reviewed_worker_id,
        )

    @mcp.tool(tags={"fleet"})
    async def fleet_agents(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """List what this fleet run's workers can be: on-server agents, and models.

        "agents": on-server agents a worker can run as. Each has a name, a short
        description, the backend and model its own config runs it on, and a
        rough list price per million tokens. Pass the name as fleet_spawn's agent.
        "models": the models a task worker (a copy of you, e.g. "fork yourself
        across models") may run on, each with a plain name and its id. Pass
        either as fleet_spawn's model. The agents' models are not this list.
        Every spawn checks again, so both lists are advice. A model the
        person asked for that isn't listed here isn't available: say so and
        ask which listed model to use, rather than picking one for them.
        "family_names": a family name alone ("haiku", "sonnet", "luna",
        "flash") means the newest model of that family, e.g. haiku ->
        claude-haiku-5-5. Pass it as said and name the exact model in your
        reply; never ask which version. Ask only when the name matches no
        listed model, or models of different families.
        """
        return dict(await owner().agents(await _auth(ctx), origin_context_id=origin_context_id))

    @mcp.tool(tags={"fleet"})
    async def fleet_collect(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID | None = None,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Collect verified outputs only after contracts and required reviews pass.

        The host returns waiting or blocked when evidence is incomplete. Status
        excerpts, a reached quorum, or a wake are not result certificates.
        A blocked run with partial=true still carries the verified results of
        the workers that succeeded and lists the failed ones: present it as a
        partial result and name which worker failed, never as a complete one.
        A ready or partial result carries `cost`: end your reply with it verbatim.
        Omit run_id for this turn's run.
        """
        return dict(
            await owner().collect(
                await _auth(ctx), run_id=run_id, origin_context_id=origin_context_id
            )
        )
