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
)
from daimon.adapters.mcp.tools.setup_target import resolve_setup_agent
from daimon.core.defaults.mcp_merge import get_reserved_mcp_rejection
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.skills import resolve_custom_skill_titles
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.operation_policy import TargetFacts, decide_operation, needs_reachability_read
from daimon.core.stores.agent_files import delete_agent_file, list_agent_files
from daimon.core.stores.agent_mcp_credentials import (
    delete_credential as delete_agent_mcp_credential,
)
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
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
    expected_ma_agent_id: str | None = None,
) -> AgentInfo:
    # Reserved-name check first and unconditionally — before any agent lookup
    # or I/O, mirroring _attach_mcp_server_impl's #142 guard.
    rejection = get_reserved_mcp_rejection(server_name=server_name, url="", public_url=None)
    if rejection is not None:
        raise ToolError(rejection)

    agent = await resolve_setup_agent(
        runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
    )
    # `mcp_remove` sits in `decide_operation`'s attachment family, not the spec
    # family `_reject_system_agent` enforces: the token form attaches a server
    # to the seeded agent for any member, and the defaults reconciler unions
    # whatever foreign servers MA holds, so detaching one can never drift the
    # seed. Gating removal harder than the attach it undoes is what left a
    # rejected Notion token stuck on the built-in agent with no way off, even
    # for an admin. Now an admin may always detach, and a member may detach
    # from an agent nobody has scoped; a member's own attach to a shared agent
    # still needs an admin to undo, because the removal reaches everyone.
    is_daimon_managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    reachable = False
    if needs_reachability_read(
        "mcp_remove", is_admin=auth.is_admin, is_daimon_managed=is_daimon_managed
    ):
        async with runtime.session_factory() as session:
            reachable = await is_agent_reachable_in_tenant(
                session,
                tenant_id=auth.tenant_id,
                agent_name=agent_name,
                default=runtime.deployment_default,
            )
    outcome = decide_operation(
        "mcp_remove",
        is_admin=auth.is_admin,
        target=TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=reachable),
    )
    if outcome != "allow":
        raise ToolError(
            f"Disconnecting '{server_name}' from '{agent_name}' needs a workspace or server "
            "admin, and the caller is not one. Tell them an admin can ask Daimon: disconnect "
            f"{server_name} from {agent_name}. Nothing was changed. Do not retry."
        )

    existing = list(agent.mcp_servers or [])
    target_server = next((s for s in existing if s.name == server_name), None)
    if target_server is None:
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
    # The shared token the form stored for this URL is mirrored into every
    # caller's vault at session create; without this delete a re-attach at
    # the same URL would silently reuse the token that was just rejected.
    agent_id: uuid.UUID = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
    async with runtime.session_factory.begin() as session:
        await delete_agent_mcp_credential(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            mcp_server_url=target_server.url.rstrip("/"),
        )
    return await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)


async def _remove_skill_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    skill_id: str,
    expected_ma_agent_id: str | None = None,
) -> AgentInfo:
    agent = await resolve_setup_agent(
        runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
    )
    _reject_system_agent(agent)
    await reachability.require_admin_for_reachable_agent(runtime, auth, agent_name=agent_name)

    titles, _truncated = await resolve_custom_skill_titles(
        runtime.client, agents=[agent], tenant_id=auth.tenant_id
    )
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
    expected_ma_agent_id: str | None = None,
) -> list[str]:
    # Deliberately NOT reachability-gated and NOT _reject_system_agent-guarded:
    # env variables are per-agent daimon rows keyed (tenant_id, agent_id, key)
    # that never enter the MA agent spec, so neither the spec-drift guard nor
    # the approved-configuration gate applies. This is a read; the ungated-
    # reads convention (_ctx.py's _require_admin docstring) covers it too.
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=agent_name,
        expected_ma_agent_id=expected_ma_agent_id,
        require_identity=False,
    )
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
    expected_ma_agent_id: str | None = None,
) -> RemoveEnvCredentialResult:
    # Adding and removing a key are deliberately NOT symmetric. Adding is a
    # contribution: one new value, held by the requester alone, that overwrites
    # nothing — so any member may add one, to the seeded agent included. Removing
    # takes a key away from everyone who talks to the agent, and on a shared or
    # built-in agent that is the whole install's blast radius, so it needs an
    # admin. `key_remove` is in `decide_operation`'s attachment family, where the
    # admin check comes first: an admin can still remove a key from the built-in
    # agent, which is the point of gating rather than forbidding.
    #
    # _reject_system_agent and require_admin_for_reachable_agent still do not
    # apply: those guard the MA agent spec, and these keys are per-agent daimon
    # rows that never enter it.
    agent = await resolve_setup_agent(
        runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
    )
    is_daimon_managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    reachable = False
    if needs_reachability_read(
        "key_remove", is_admin=auth.is_admin, is_daimon_managed=is_daimon_managed
    ):
        async with runtime.session_factory() as session:
            reachable = await is_agent_reachable_in_tenant(
                session,
                tenant_id=auth.tenant_id,
                agent_name=agent_name,
                default=runtime.deployment_default,
            )
    outcome = decide_operation(
        "key_remove",
        is_admin=auth.is_admin,
        target=TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=reachable),
    )
    if outcome != "allow":
        raise ToolError(
            f"Removing {key} from '{agent_name}' needs a workspace or server admin, and the "
            f"caller is not one. Tell them an admin can ask Daimon: remove {key} from "
            f"{agent_name}. The key is unchanged. Do not retry."
        )
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
        expected_ma_agent_id: str | None = None,
    ) -> AgentInfo:
        """Disconnect an MCP server such as Linear or Notion from an agent. Detach
        the named connection and its tools while preserving other servers and
        skills, and forget the shared token stored for it; the change reaches the
        agent on its next message, not the one running now.

        ``attach_mcp_server`` adds public endpoints; ``request_mcp_token`` and
        ``request_mcp_oauth`` enroll authenticated connections. The built-in
        daimon server cannot be removed. Use this when a connection keeps
        failing: a server admin can disconnect from any agent, built-in Daimon
        included; a member can disconnect from an agent that is not a channel or
        workspace default."""
        return await _detach_mcp_server_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool
    async def remove_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        skill_id: str,
        expected_ma_agent_id: str | None = None,
    ) -> AgentInfo:
        """Stop an agent using an attached skill, such as eda. Remove that one
        attachment while preserving other skills; it applies from the agent's next
        message, not the one running now.

        ``delete_skill`` destroys the shared workspace skill instead;
        ``update_agent`` adds existing skills. Accept a skill name or raw ``skill_id``.
        The resource and other agents stay intact. Changing a default agent needs admin."""
        return await _remove_skill_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            skill_id=skill_id,
            expected_ma_agent_id=expected_ma_agent_id,
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
        expected_ma_agent_id: str | None = None,
    ) -> RemoveEnvCredentialResult:
        """Remove an old API key or token, such as a Toggl key, from an agent's stored keys.

        Use ``request_agent_key`` to add a key privately and ``list_agent_keys`` to
        inspect stored names. Removing a missing key also succeeds; ``removed`` says
        whether it was present. This deletes the stored environment variable and never
        returns its secret value. Removing it stops it being supplied from the next
        message; it does not cancel it at the service or stop work already using it.
        Anyone may add a key, but removing one from an agent that answers a channel
        or the whole workspace needs an admin."""
        return await _remove_agent_key_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            key=key,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool
    async def list_agent_keys(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: Annotated[
            str, Field(description="Exact name of the agent whose stored keys were requested.")
        ],
        expected_ma_agent_id: Annotated[
            str | None,
            Field(description="ID returned by get_agent for that same named agent, when known."),
        ] = None,
    ) -> list[str]:
        """What keys does an agent have? List its stored API key names, never values.

        Pass the named agent, even when another agent is answering. The current
        session's .env cannot establish that agent's keys. Use ``get_agent`` to
        resolve its identity or inspect MCP access. Stored keys do not prove
        availability in this session. Use ``request_agent_key`` to add keys."""
        return await _list_agent_keys_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
        )
