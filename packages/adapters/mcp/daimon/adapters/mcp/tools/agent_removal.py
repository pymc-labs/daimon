"""Agent removal tools: detach_mcp_server, remove_skill, remove_agent_key,
list_agent_keys.

``register_agent_removal_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures
for this group; each closure delegates to a module-private ``_*_impl``
coroutine that can be unit-tested without a FastMCP Context.

A sibling of ``agents.py`` rather than more of it: these four tools close the
last gap between what other setup surfaces can do to an agent and what
chatting with the agent can do — detach an attached MCP server, detach an
attached skill, remove an env variable, and enumerate the env variable key
names currently set (never their values).
"""

from __future__ import annotations

import uuid
from typing import Annotated, cast

import anthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import reachability
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.agents import (
    AgentInfo,
    _build_agent_info,  # pyright: ignore[reportPrivateUsage]
    _ma_tool_to_param,  # pyright: ignore[reportPrivateUsage]
    _reject_system_agent,  # pyright: ignore[reportPrivateUsage]
    _resolve_custom_skill_titles,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.mcp_merge import get_reserved_mcp_rejection
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_files import delete_agent_file, list_agent_files
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field


class RemoveEnvCredentialResult(BaseModel):
    """Result of ``remove_agent_key``. Never carries a value."""

    model_config = ConfigDict(frozen=True)

    agent_name: str
    key: str
    removed: bool
    """True if the key was present and deleted; False if it was already absent
    (the delete is idempotent, so the call still succeeds either way)."""


async def _detach_mcp_server_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
) -> AgentInfo:
    # Reserved-name check first and unconditionally — before any agent lookup
    # or I/O, mirroring _attach_mcp_server_impl's #142 guard.
    rejection = get_reserved_mcp_rejection(server_name=server_name, url="", public_url=None)
    if rejection is not None:
        raise ToolError(rejection)

    agent = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if agent is None:
        raise ToolError(f"agent '{agent_name}' not found")
    _reject_system_agent(agent)
    await reachability.require_admin_for_reachable_agent(runtime, auth, agent_name=agent_name)

    existing = list(agent.mcp_servers or [])
    if not any(s.name == server_name for s in existing):
        attached = ", ".join(s.name for s in existing) or "none"
        raise ToolError(
            f"'{server_name}' is not attached to '{agent_name}'. Currently attached: {attached}"
        )

    # Version-retry closure: mcp_servers and the matching mcp_toolset tool
    # entry are recomputed from `fresh` on every attempt and removed together
    # — MA rejects an agent whose mcp_servers aren't each referenced by a
    # mcp_toolset, so leaving one behind would make the update inconsistent.
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        fresh_existing = list(fresh.mcp_servers or [])
        new_mcp_servers: list[BetaManagedAgentsURLMCPServerParams] = [
            {"name": s.name, "type": "url", "url": s.url}
            for s in fresh_existing
            if s.name != server_name
        ]
        new_tools: list[Tool] = []
        for t in fresh.tools:
            if t.type == "mcp_toolset" and t.mcp_server_name == server_name:
                continue
            new_tools.append(_ma_tool_to_param(t))
        return await runtime.client.beta.agents.update(
            fresh.id,
            version=fresh.version,
            mcp_servers=new_mcp_servers,
            tools=new_tools,
        )

    try:
        updated = await update_agent_with_version_retry(runtime.client, agent.id, _apply)
    except anthropic.ConflictError as exc:
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    return await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)


async def _remove_skill_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    skill_id: str,
) -> AgentInfo:
    agent = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if agent is None:
        raise ToolError(f"agent '{agent_name}' not found")
    _reject_system_agent(agent)
    await reachability.require_admin_for_reachable_agent(runtime, auth, agent_name=agent_name)

    titles = await _resolve_custom_skill_titles(runtime.client, [agent], tenant_id=auth.tenant_id)
    target_id: str | None = None
    for sk in agent.skills:
        if sk.skill_id == skill_id or titles.get(sk.skill_id) == skill_id:
            target_id = sk.skill_id
            break
    if target_id is None:
        listing = (
            ", ".join(
                f"{sk.skill_id} ({titles[sk.skill_id]})" if sk.skill_id in titles else sk.skill_id
                for sk in agent.skills
            )
            or "none"
        )
        raise ToolError(
            f"'{skill_id}' is not attached to '{agent_name}'. Currently attached: {listing}"
        )

    # Version-retry closure: only `skills` is recomputed from `fresh` and
    # patched. `tools` is left untouched — the base toolset must stay whether
    # or not skills remain attached.
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        new_skills: list[BetaManagedAgentsSkillParams] = [
            cast(BetaManagedAgentsSkillParams, {"skill_id": sk.skill_id, "type": sk.type})
            for sk in fresh.skills
            if sk.skill_id != target_id
        ]
        return await runtime.client.beta.agents.update(
            fresh.id, version=fresh.version, skills=new_skills
        )

    try:
        updated = await update_agent_with_version_retry(runtime.client, agent.id, _apply)
    except anthropic.ConflictError as exc:
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    return await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)


async def _list_agent_keys_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
) -> list[str]:
    # Deliberately NOT reachability-gated and NOT _reject_system_agent-guarded:
    # env variables are per-agent daimon rows keyed (tenant_id, agent_id, key)
    # that never enter the MA agent spec, so neither the spec-drift guard nor
    # the approved-configuration gate applies. This is a read; the ungated-
    # reads convention (_ctx.py's _require_admin docstring) covers it too.
    agent = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if agent is None:
        raise ToolError(f"agent '{agent_name}' not found")
    agent_id: uuid.UUID = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
    async with runtime.session_factory() as session:
        rows = await list_agent_files(session, tenant_id=auth.tenant_id, agent_id=agent_id)
    return [row.key for row in rows]


async def _remove_agent_key_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    key: str,
) -> RemoveEnvCredentialResult:
    # Same rationale as _list_agent_keys_impl above: env variables
    # are per-agent daimon rows outside the MA spec, so neither
    # _reject_system_agent nor require_admin_for_reachable_agent applies here
    # — and the existing chat credential button already writes these rows for
    # the seeded agent, so gating removal would leave a write with no
    # matching delete.
    agent = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if agent is None:
        raise ToolError(f"agent '{agent_name}' not found")
    agent_id: uuid.UUID = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
    async with runtime.session_factory.begin() as session:
        # The store delete is idempotent (no raise when absent) — read first
        # so the result can report a truthful removed flag instead of an
        # unconditional success.
        rows = await list_agent_files(session, tenant_id=auth.tenant_id, agent_id=agent_id)
        was_present = any(row.key == key for row in rows)
        await delete_agent_file(session, tenant_id=auth.tenant_id, agent_id=agent_id, key=key)
    return RemoveEnvCredentialResult(agent_name=agent_name, key=key, removed=was_present)


def register_agent_removal_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def detach_mcp_server(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: Annotated[
            str, Field(description="Name of the attached MCP server to disconnect.")
        ],
    ) -> AgentInfo:
        """Disconnect an MCP server such as Linear from an agent. Detach the named
        connection and its tools while preserving other servers and skills.

        ``attach_mcp_server`` adds public endpoints; ``request_mcp_token`` enrolls
        authenticated connections. The built-in daimon server cannot be removed.
        Changing a channel or workspace default needs admin."""
        return await _detach_mcp_server_impl(
            runtime, await _auth(ctx), agent_name=agent_name, server_name=server_name
        )

    @mcp.tool
    async def remove_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        skill_id: str,
    ) -> AgentInfo:
        """Stop an agent using an attached skill, such as eda. Remove that one
        attachment while preserving other skills.

        ``delete_skill`` destroys the shared workspace skill instead;
        ``update_agent`` adds existing skills. Accept a skill name or raw ``skill_id``.
        The resource and other agents stay intact. Changing a default agent needs admin."""
        return await _remove_skill_impl(
            runtime, await _auth(ctx), agent_name=agent_name, skill_id=skill_id
        )

    @mcp.tool
    async def remove_agent_key(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        key: Annotated[
            str,
            Field(
                description=(
                    "Stored environment variable name, UPPER_SNAKE, e.g. TOGGL_TOKEN "
                    "or OPENAI_API_KEY; never the secret value."
                )
            ),
        ],
    ) -> RemoveEnvCredentialResult:
        """Remove an old API key or token, such as a Toggl key, from an agent's stored keys.

        Use ``request_agent_key`` to add a key privately and ``list_agent_keys`` to
        inspect stored names. Removing a missing key also succeeds; ``removed`` says
        whether it was present. This deletes the stored environment variable, never
        returns its secret value, and does not prove an existing session refreshed."""
        return await _remove_agent_key_impl(
            runtime, await _auth(ctx), agent_name=agent_name, key=key
        )

    @mcp.tool
    async def list_agent_keys(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
    ) -> list[str]:
        """What keys does an agent have? List its stored API key names, never values.

        Use ``get_agent`` for skills and MCP access, ``request_agent_key`` to add
        keys or ``remove_agent_key`` to remove them. Stored names on this target
        are not proof that the answering agent can use those keys."""
        return await _list_agent_keys_impl(runtime, await _auth(ctx), agent_name=agent_name)
