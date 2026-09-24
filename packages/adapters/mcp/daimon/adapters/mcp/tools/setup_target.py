"""Authenticated turn origins and identity-pinned configuration targets."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.thread_agent_bindings import get_binding, update_target
from daimon.core.stores.turn_origins import get_active_origin, update_origin_target
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def require_turn_origin(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin_context_id: str | None,
) -> TurnOriginRow:
    if not origin_context_id or auth.platform not in ("discord", "slack"):
        raise ToolError("Use the origin_context_id from this active platform turn.")
    try:
        origin_id = uuid.UUID(origin_context_id)
    except ValueError as exc:
        raise ToolError(
            "The turn origin is invalid; retry from the originating conversation."
        ) from exc
    async with runtime.session_factory() as session:
        origin = await get_active_origin(
            session,
            origin_id=origin_id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            now=datetime.now(UTC),
        )
    if origin is None:
        raise ToolError("This turn origin is unavailable or expired; retry in that conversation.")
    if auth.agent_id is not None and auth.agent_id != derive_agent_uuid(
        tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id
    ):
        raise ToolError("This turn origin belongs to another responder.")
    return origin


async def resolve_setup_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    name: str,
    expected_ma_agent_id: str | None = None,
    require_identity: bool = True,
) -> BetaManagedAgentsAgent:
    """Resolve a name without adopting an ambiguous or recreated namesake."""
    if require_identity and auth.platform in ("discord", "slack") and expected_ma_agent_id is None:
        raise ToolError(
            "Pass expected_ma_agent_id from the selected target or list_agents before acting. "
            "Which current agent should I configure? Nothing was changed."
        )
    agents = await find_agents_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if expected_ma_agent_id is not None:
        for agent in agents:
            if agent.id == expected_ma_agent_id:
                return agent
        raise ToolError(
            f"The selected agent '{name}' ({expected_ma_agent_id}) is missing or changed. "
            "Which current agent should I configure? Nothing was changed."
        )
    if len(agents) != 1:
        raise ToolError(
            f"agent '{name}' {'not found' if not agents else 'is ambiguous'}. "
            "Which current agent should I configure? Nothing was changed."
        )
    return agents[0]


async def _set_setup_target_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    agent_id: str,
) -> TurnOriginRow:
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    target = next(
        (
            agent
            for agent in await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
            if agent.id == agent_id
        ),
        None,
    )
    if target is None:
        raise ToolError(
            "That agent is missing from this tenant. Which current agent should I configure?"
        )
    target_name = target.metadata.get(MA_METADATA_KEY_NAME)
    if not target_name:
        raise ToolError(
            "That agent has no configuration name. Select a named agent in this tenant."
        )
    async with runtime.session_factory.begin() as session:
        active_origin = await get_active_origin(
            session,
            origin_id=origin.id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=origin.platform,
            now=datetime.now(UTC),
            for_update=True,
        )
        if active_origin is None:
            raise ToolError("This turn origin expired; retry in the setup conversation.")
        binding = await get_binding(
            session,
            tenant_id=origin.tenant_id,
            platform=origin.platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
        )
        if binding is None or binding.deleted:
            raise ToolError(
                "This thread is not a setup conversation, so there is no selected "
                "target to switch. That does not block configuring the agent: name "
                "it and use update_agent or a request tool directly. A setup "
                "conversation is opened with the Manage agents button in the "
                "setup panel. Nothing was changed."
            )
        if binding.kind == "handoff":
            raise ToolError(
                f"This is a handoff conversation: {binding.responder_name} answers here "
                "because this task was handed to it, so there is no setup target to "
                "change. Use Manage agents to pick an agent. Nothing was changed."
            )
        updated_binding = await update_target(
            session,
            tenant_id=origin.tenant_id,
            platform=origin.platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            configuration_target_ma_agent_id=agent_id,
            configuration_target_name=target_name,
        )
        if updated_binding is None:
            raise ToolError("The setup conversation was deleted. Open a new setup conversation.")
        return await update_origin_target(
            session,
            origin_id=origin.id,
            configuration_target_ma_agent_id=agent_id,
            configuration_target_name=target_name,
        )


def register_setup_target_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def set_setup_target(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        agent_id: str,
    ) -> TurnOriginRow:
        """Select which agent to configure in an existing setup conversation.

        Pass the current turn's origin_context_id and the selected agent's current
        id from list_agents. The responder stays the same: this changes only the
        configuration target, not who answers or whose keys the session can use.
        Setup conversations keep Daimon as their responder. In ordinary task
        threads, use hand_off_task to change the responder. Use set_agent_default
        to change a channel's default responder.

        Ordinary threads have no setup target to switch. Configure a named agent
        there by passing its name and current id directly to the relevant tool.
        State a successful target switch briefly. Other running turns keep their
        own target. This changes no editing permissions.
        """
        return await _set_setup_target_impl(
            runtime, await _auth(ctx), origin_context_id=origin_context_id, agent_id=agent_id
        )
