"""Per-target runtime gate: is the named agent currently reachable in this tenant?

Blast-radius rule for whoever adds the next tool: **blast radius of one
agent -> open; blast radius of the whole tenant -> admin.** Creating, forking,
or configuring an agent nobody has scoped only ever affects that one agent, so
it stays open to any member. The moment an agent is a channel or workspace
default, changing the fields that shape what it says and does reaches every
user who talks to it — that requires admin.

``description`` is deliberately not in the gated set: it is a label, not
something that changes what the agent does or can reach. ``tools`` IS in the
gated set even though ``mcp_servers`` might look like the more obvious target:
an ``mcp_toolset`` entry in ``tools`` is how an attached MCP server actually
becomes reachable by the model, so gating ``mcp_servers`` while leaving
``tools`` open would let a non-admin route around the gate entirely — it
would be no gate at all.
"""

from __future__ import annotations

from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from fastmcp.exceptions import ToolError

REACHABILITY_GATED_FIELDS: Final[frozenset[str]] = frozenset(
    {"system", "model", "skills", "mcp_servers", "tools"}
)


async def require_admin_for_reachable_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
) -> None:
    """Raise ``ToolError`` if a non-admin caller is patching a currently-reachable agent.

    Returns immediately for an admin caller without touching the database.
    For a non-admin caller, reads the tenant's config cascade and raises when
    ``agent_name`` currently resolves for any user in this tenant (channel,
    tenant, or deployment tier). ``tenant_id`` always comes from
    ``auth.tenant_id`` — never from a caller-supplied parameter — so a caller
    cannot point the read at another tenant's config rows.
    """
    if auth.is_admin:
        return
    async with runtime.session_factory() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=auth.tenant_id,
            agent_name=agent_name,
            default=runtime.deployment_default,
        )
    if reachable:
        raise ToolError(
            f"'{agent_name}' is currently the default agent for this workspace or a "
            "channel, so an admin must change its setup. Tell the caller to ask a workspace "
            f"or server admin to make the requested change to '{agent_name}'; carry the "
            "specific action from the conversation into that handoff. Do not retry. "
            "Creating or forking your own agent is not gated."
        )
