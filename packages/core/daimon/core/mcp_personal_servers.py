"""Which of an agent's MCP servers a given caller can actually authenticate.

An OAuth sign-in is personal: the grant lands in the connecting person's own
(account, agent) vault, but `attach_mcp_server_to_agent` puts the server on
the agent everyone shares. MA opens every attached server on every turn and
resolves its credential from the vault mounted on that session — the
caller's — so one person signing in left every other person's turn opening a
server they have no token for, failing it with
`mcp_authentication_failed_error`, and carrying the degraded-turn notice
under every reply whether or not the turn had anything to do with that
server.

`hidden_mcp_server_names` names the servers to leave out of one caller's
session; `visible_mcp_servers` and `visible_tools` cut them out of the two
arrays MA insists move together — an `mcp_toolset` whose server is gone is
rejected, as is a server no toolset references.

Pure, and deliberately import-free at runtime: `session_snapshot` hashes what
these return, and `stores.domain` imports `session_snapshot`, so a store
import here would close a cycle. The grant rows are read by
`agent_mcp_credentials.resolve_hidden_mcp_server_names` and passed in.

A server whose credential is agent-wide (`agent_mcp_credentials`, mirrored
into every caller's vault at session create) is never personal, even when
somebody also signed in to it personally: everyone can authenticate it.

Otherwise a URL is a sign-in server as soon as anyone in the tenant has
signed in to it, on any agent: whether a server wants a sign-in is a
property of the server, not of the agent it is attached to. A grant counts
only for the agent it was made on, because MA authenticates from the vault
of (caller, that agent) and nothing else fills it — so a fork, which copies
its source's servers and none of the sign-ins, hides them from everyone
(the person who signed in on the source included) until someone signs in
on the fork. Matching is by URL, which is what MA authenticates against; a
name re-pointed at a different server does not let a grant for the old one
pass for a connection to the new. The one shape this misreads is a server
that accepts anonymous callers *and* offers a sign-in for more: after one
member signs in, the anonymous access is hidden from the rest.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_agent import Tool as MATool
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_mcp_toolset import BetaManagedAgentsMCPToolset

# A dependency-free leaf, so it does not close the import cycle described above.
from daimon.core.mcp_server_url import canonical_mcp_url

if TYPE_CHECKING:
    from daimon.core.stores.domain import McpOAuthGrantRow

__all__ = ["hidden_mcp_server_names", "visible_mcp_servers", "visible_tools"]


def hidden_mcp_server_names(
    grants: Iterable[McpOAuthGrantRow],
    *,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    shared_server_urls: Iterable[str],
    server_urls: Mapping[str, str],
) -> frozenset[str]:
    """Of `server_urls`, the ones people sign in to that this caller has not
    signed in to on this agent.

    Pure. `server_urls` is the agent's own `{name: url}`; `grants` is the
    tenant's, so a sign-in on any agent marks the URL as one that needs a
    sign-in, and only a grant by this caller on this agent satisfies it — see
    the module docstring for why. Trailing slashes are ignored, as everywhere
    a server URL is compared.
    """
    signed_in: set[str] = set()
    mine: set[str] = set()
    for grant in grants:
        grant_url = canonical_mcp_url(grant.mcp_server_url)
        signed_in.add(grant_url)
        if grant.agent_id == agent_id and grant.account_id == account_id:
            mine.add(grant_url)
    hidden_urls = signed_in - mine - {canonical_mcp_url(url) for url in shared_server_urls}
    return frozenset(
        name for name, url in server_urls.items() if canonical_mcp_url(url) in hidden_urls
    )


def visible_mcp_servers(
    agent: BetaManagedAgentsAgent, hidden: frozenset[str]
) -> Sequence[BetaManagedAgentsMCPServerURLDefinition]:
    """The agent's MCP servers without the ones `hidden` names."""
    return [server for server in agent.mcp_servers if server.name not in hidden]


def visible_tools(agent: BetaManagedAgentsAgent, hidden: frozenset[str]) -> Sequence[MATool]:
    """The agent's tools without the toolsets of the servers `hidden` names."""
    return [
        tool
        for tool in agent.tools
        if not (isinstance(tool, BetaManagedAgentsMCPToolset) and tool.mcp_server_name in hidden)
    ]
