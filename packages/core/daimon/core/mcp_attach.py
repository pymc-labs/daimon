"""Attach an external MCP server to an MA agent's spec.

Two adapters need this and neither may import the other: the MCP tool
``attach_mcp_server`` and the Discord credential modal, which must attach the
server it has just written a vault credential for. Before this module existed
only the MCP tool could do it, so the credential flow stored a token for a
server the agent was never told about and reported success anyway.

MA rejects an agent whose ``mcp_servers`` entries are not each referenced by a
matching ``mcp_toolset`` in ``tools``, so the two lists must move together —
which is the whole reason this is one function rather than a caller-assembled
pair of updates.

Replacing a server the agent already declares under the same name at a
different URL repoints every caller's traffic for that server, so it is an
attachment write (`mcp_replace`): each caller decides it with
`decide_mcp_replacement` and passes the answer as `replace_allowed`. The write
re-checks against the freshly-retrieved agent, so a server attached between
the caller's check and the update is not silently replaced.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_agent import Tool as MATool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core.agent_pins import agent_pin_names
from daimon.core.agent_reach import load_target_facts
from daimon.core.channel_admins import ChannelAdminCaller
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mcp_server_url import same_mcp_url
from daimon.core.operation_policy import (
    PolicyOutcome,
    TargetFacts,
    decide_operation,
    needs_reachability_read,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_mcp_credentials as cred_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT_MCP_TOOLSET_CONFIG: Final[dict[str, Any]] = {
    "permission_policy": {"type": "always_allow"},
}


class McpServerReplaceRefusedError(DaimonError):
    """The agent already has this server at another URL and the caller may not repoint it."""

    def __init__(self, *, server_name: str) -> None:
        super().__init__(
            f"The agent already has an MCP server named '{server_name}' at a different URL. "
            "Replacing it needs a server or workspace admin. Nothing was changed."
        )
        self.server_name = server_name


def replaced_server_url(agent: BetaManagedAgentsAgent, *, server_name: str, url: str) -> str | None:
    """The URL ``server_name`` points at today when attaching ``url`` would change it.

    ``None`` for a new name, or for the same name at the same URL (compared in
    `canonical_mcp_url` form, as the vault does).
    """
    for server in agent.mcp_servers or []:
        if server.name == server_name and not same_mcp_url(server.url, url):
            return server.url
    return None


async def decide_mcp_replacement(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent: BetaManagedAgentsAgent,
    caller: ChannelAdminCaller,
    default: DeploymentDefault,
) -> PolicyOutcome:
    """Attachment-family decision for replacing one of ``agent``'s MCP servers.

    Admin: allowed. Otherwise refused on a defaults-managed agent or one that
    is shared for key changes (`is_agent_shared_for_key_changes`), unless the
    caller administers every channel it answers and runs in; allowed on a
    rule-bound agent local to administered channels. Every routine and live
    session counts, the caller's own included. The facts are read only when the
    answer depends on them.
    """
    is_daimon_managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    facts = TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)
    if needs_reachability_read(
        "mcp_replace", is_admin=caller.is_server_admin, is_daimon_managed=is_daimon_managed
    ):
        async with session_factory() as session:
            facts = await load_target_facts(
                session,
                "mcp_replace",
                tenant_id=tenant_id,
                platform=platform,
                agent_names=agent_pin_names(agent.name, agent.metadata),
                ma_agent_id=str(agent.id),
                default=default,
                caller=caller,
                is_daimon_managed=is_daimon_managed,
            )
    return decide_operation("mcp_replace", is_admin=caller.is_server_admin, target=facts)


@dataclass(frozen=True, slots=True)
class McpConnectDecision:
    """Whether this caller may attach a server and replace existing shared state."""

    replaces: bool
    replace_allowed: bool
    connect_allowed: bool = True
    attaches: bool = True

    @property
    def refused(self) -> bool:
        return not self.connect_allowed or (self.replaces and not self.replace_allowed)


async def decide_mcp_connect(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent: BetaManagedAgentsAgent,
    agent_id: uuid.UUID,
    server_name: str,
    url: str,
    platform: str,
    caller: ChannelAdminCaller,
    default: DeploymentDefault,
    shares_token: bool,
) -> McpConnectDecision:
    """Decide connecting ``server_name`` at ``url`` to ``agent`` for this caller.

    Two things count as replacing: repointing a server name the agent already
    declares at another URL, and, when ``shares_token`` (a pasted token becomes
    the agent-wide credential every caller's session mirrors), setting the
    token for a URL the agent already uses or already has a token for. An
    OAuth grant lands only in the requester's own vault, so only the first
    applies to it. Pass
    ``replace_allowed`` on to `attach_mcp_server_to_agent`, which re-checks
    the first against the fresh agent. Every connection checks mutation ownership,
    including connections using a new name.
    """
    replaces = replaced_server_url(agent, server_name=server_name, url=url) is not None
    if (
        not shares_token
        and any(s.name == server_name and same_mcp_url(s.url, url) for s in agent.mcp_servers or [])
        and any(t.type == "mcp_toolset" and t.mcp_server_name == server_name for t in agent.tools)
    ):
        # Signing into an already-declared server changes only this person's
        # vault. No agent mutation is needed or authorized by this exception.
        return McpConnectDecision(replaces=False, replace_allowed=False, attaches=False)
    if not replaces and shares_token:
        # A pasted token becomes the credential every caller's session uses for
        # this URL. If any server on the agent already points there, people
        # are already using it (through their own grant, or none), so the
        # first shared token is a replacement too, not only an overwrite.
        replaces = any(same_mcp_url(server.url, url) for server in agent.mcp_servers or [])
    if not replaces and shares_token:
        async with session_factory() as session:
            rows = await cred_store.list_credentials(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        replaces = any(same_mcp_url(row.mcp_server_url, url) for row in rows)
    # Even a new name changes the shared agent's spec. A private token or
    # OAuth grant does not confer permission to attach a server to that agent.
    outcome = await decide_mcp_replacement(
        session_factory,
        tenant_id=tenant_id,
        platform=platform,
        agent=agent,
        caller=caller,
        default=default,
    )
    return McpConnectDecision(
        replaces=replaces,
        replace_allowed=replaces and outcome == "allow",
        connect_allowed=outcome == "allow",
    )


def _ma_tool_to_param(tool: MATool) -> Tool:
    """Dump an MA response Tool to a Params dict suitable for the SDK update body."""
    return cast(Tool, tool.model_dump(mode="json", exclude_none=True))


def build_attached_spec(
    agent: BetaManagedAgentsAgent, *, server_name: str, url: str
) -> tuple[list[BetaManagedAgentsURLMCPServerParams], list[Tool]]:
    """Return ``(mcp_servers, tools)`` with ``server_name`` attached at ``url``.

    Pure. An entry with the same ``server_name`` is replaced (last-write-wins)
    and every other server, toolset, and tool is preserved. The matching
    ``mcp_toolset`` is appended only when absent, so re-attaching an existing
    server does not accumulate duplicate toolsets.

    Takes the agent it should compute against as an argument rather than
    reading one, so a version-retry can recompute from the freshly-retrieved
    agent instead of a stale read.
    """
    servers: list[BetaManagedAgentsURLMCPServerParams] = [
        {"name": s.name, "type": "url", "url": s.url}
        for s in (agent.mcp_servers or [])
        if s.name != server_name
    ]
    servers.append({"name": server_name, "type": "url", "url": url})

    tools: list[Tool] = [_ma_tool_to_param(t) for t in agent.tools]
    if not any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == server_name for t in tools
    ):
        tools.append(
            cast(
                Tool,
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": server_name,
                    "default_config": DEFAULT_MCP_TOOLSET_CONFIG,
                },
            )
        )
    return servers, tools


async def attach_mcp_server_to_agent(
    client: AsyncAnthropic,
    agent_id: str,
    *,
    server_name: str,
    url: str,
    replace_allowed: bool,
    shares_token: bool = False,
    before_update: Callable[[], Awaitable[None]] | None = None,
) -> BetaManagedAgentsAgent:
    """Attach ``server_name`` at ``url`` to ``agent_id``, preserving the rest.

    ``before_update`` is the caller's access decision, awaited after each
    fresh retrieve and immediately before the update (retries included). It
    raises to refuse, and nothing is written.

    Retrieves the agent, recomputes both lists from that fresh read, and
    updates. ``anthropic.ConflictError`` propagates after the single retry
    ``update_agent_with_version_retry`` performs — callers at an adapter
    boundary decide how to surface it.

    Callers are responsible for any policy gate (reserved-server rejection,
    admin checks). ``replace_allowed`` is the caller's `mcp_replace` decision:
    when it is False and the fresh agent already has ``server_name`` at another
    URL, raise `McpServerReplaceRefusedError` and write nothing. With
    ``shares_token`` (a pasted token that became the agent-wide credential),
    a server already at ``url`` under any name is refused too: it was
    attached after the caller decided, and its users would now run on the
    caller's token. The caller withdraws that token.
    """

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        if not replace_allowed and (
            replaced_server_url(fresh, server_name=server_name, url=url)
            or (
                shares_token
                and any(same_mcp_url(server.url, url) for server in fresh.mcp_servers or [])
            )
        ):
            raise McpServerReplaceRefusedError(server_name=server_name)
        servers, tools = build_attached_spec(fresh, server_name=server_name, url=url)
        if before_update is not None:
            await before_update()
        return await client.beta.agents.update(
            fresh.id, version=fresh.version, mcp_servers=servers, tools=tools
        )

    return await update_agent_with_version_retry(client, agent_id, _apply)
