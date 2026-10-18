"""Opt-in fleet tools over the authorized host orchestration service.

These tools accept resource handles, never authority. The host service resolves
current root membership, authorization, attenuation, reservations and operations;
it also owns result verification and the review barrier. Ordinary agent-chat
entry points retain their independent outside-chat restriction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Protocol

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.fleet_orchestration import FleetOrchestrator
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ResourceRef, Revision
from mux.contracts.resources import AgentPatch, AgentSpec, SessionSpec
from pydantic import Field, JsonValue

if TYPE_CHECKING:
    from daimon.core.neutral_resources import WorkerPermissions
    from daimon.core.turn.deps import TurnDeps

PositiveMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
PositiveCount = Annotated[int, Field(gt=0, strict=True)]
OperationKey = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]


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
        backend: BackendConfig,
        agent: AgentSpec | None,
        clone: ResourceRef | None,
        clone_revision: Revision | None,
        overrides: AgentPatch | None,
        session: SessionSpec | None,
        budget_usd: Decimal,
        deadline: datetime,
        max_turns: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]: ...

    async def status(
        self,
        caller: AuthIdentity,
        *,
        run_id: uuid.UUID,
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
        run_id: uuid.UUID,
        origin_context_id: uuid.UUID | None = None,
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
        backend: BackendConfig,
        agent: AgentSpec | None,
        clone: ResourceRef | None,
        clone_revision: Revision | None,
        overrides: AgentPatch | None,
        session: SessionSpec | None,
        budget_usd: Decimal,
        deadline: datetime,
        max_turns: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id
        )
        from daimon.core.neutral_resources import WorkerLimits

        return await self._orchestrator.spawn(
            parent,
            policy,
            now=datetime.now(UTC),
            run_id=run_id,
            operation_key=operation_key,
            role=role,
            backend=backend,
            limits=WorkerLimits(
                budget_usd=budget_usd,
                deadline=deadline,
                max_turns=max_turns,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
            ),
            agent=agent,
            clone=clone,
            clone_revision=clone_revision,
            overrides=overrides,
            session=session,
            result_contract=result_contract,
            reviewer_of=reviewer_of,
        )

    async def status(
        self, caller: AuthIdentity, *, run_id: uuid.UUID, origin_context_id: uuid.UUID | None = None
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id, drain=True
        )
        return await self._orchestrator.status(parent, policy, run_id=run_id)

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
        self, caller: AuthIdentity, *, run_id: uuid.UUID, origin_context_id: uuid.UUID | None = None
    ) -> Mapping[str, JsonValue]:
        parent, policy = await self._parent(
            caller, origin_context_id=origin_context_id, run_id=run_id, drain=True
        )
        return await self._orchestrator.collect(parent, policy, run_id=run_id)


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
    if runtime.settings.turn.path != "mux" or not runtime.settings.turn.channel_backends:
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
        backend: BackendConfig,
        agent: AgentSpec | None,
        clone: ResourceRef | None,
        clone_revision: Revision | None,
        overrides: AgentPatch | None,
        session: SessionSpec | None,
        budget_usd: Decimal,
        deadline: datetime,
        max_turns: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        if (agent is None) == (clone is None):
            raise ToolError("Choose an agent spec or a pinned clone, exactly one.")
        if clone is None and (clone_revision is not None or overrides is not None):
            raise ToolError("Clone revisions and overrides require a clone.")
        if clone is not None and (clone.kind != "agent" or clone_revision is None):
            raise ToolError("A clone requires an agent resource and its current revision.")
        if deadline.tzinfo is None or deadline.utcoffset() is None:
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
            )
        )

    @mcp.tool(tags={"fleet"})
    async def fleet_spawn(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        operation_key: OperationKey,
        role: str,
        backend: BackendConfig,
        budget_usd: PositiveMoney,
        deadline: datetime,
        max_turns: PositiveCount = 1,
        max_input_tokens: PositiveCount | None = None,
        max_output_tokens: PositiveCount | None = None,
        run_id: uuid.UUID | None = None,
        agent: AgentSpec | None = None,
        clone: ResourceRef | None = None,
        clone_revision: Revision | None = None,
        overrides: AgentPatch | None = None,
        session: SessionSpec | None = None,
        result_contract: dict[str, JsonValue] | None = None,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Spawn a bounded worker through the root's current derived authority.

        Name the live turn's origin_context_id from its turn controls.
        Supply a fresh agent spec or an authorized clone and sparse overrides.
        A handle or role never grants permission. Retries retain operation_key.
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
            reviewer_of=None,
        )

    @mcp.tool(tags={"fleet"})
    async def fleet_status(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Read the authorized run's durable worker status; do not start work."""
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
    async def fleet_collect(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        run_id: uuid.UUID,
        origin_context_id: uuid.UUID | None = None,
    ) -> dict[str, JsonValue]:
        """Collect verified outputs only after contracts and required reviews pass.

        The host returns waiting or blocked when evidence is incomplete. Status
        excerpts, a reached quorum, or a wake are not result certificates.
        """
        return dict(
            await owner().collect(
                await _auth(ctx), run_id=run_id, origin_context_id=origin_context_id
            )
        )
