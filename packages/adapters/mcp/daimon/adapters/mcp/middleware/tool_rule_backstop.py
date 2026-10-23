"""Server-side backstop for agent tool rules on Daimon's own tools.

The turn driver skips rules for the trusted `daimon-mcp` server, and the
session's toolset for it is `always_allow`, so a stored `deny` on a Daimon tool
would never stop a call. Chat sessions also reach most Daimon tools through the
`call_tool` proxy, which the session sees as one allowed tool. `IdentityMiddleware`
runs again with the inner tool's name for a proxied call, so one check keyed on
the name being invoked covers direct and proxied calls.

An agent-wide `deny` on `daimon-mcp/*` (`WILDCARD_TOOL_KEY`) denies the whole
server: every call from that agent's chat credential is refused, `search_tools`
and `call_tool` included. A deny on one tool never refuses the two proxies by
name; the inner call it carries is refused instead.

Only an agent-wide `deny` is enforced here. `allow` has no server-side effect,
and `ask` and channel rules need the driver (the server has no card and does not
know the channel). The chat credential is shared by chat turns and routines, and
the driver denies in both (a deny ignores `scope` and `attended`), so this does too.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._words import admin_noun
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import same_mcp_url
from daimon.core.stores import agent_tool_rules
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolSafetyPolicy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Audit reason for a call this backstop refused.
DENIAL_REASON: Final = "agent_tool_rule_deny"

#: An agent-wide `deny` on this key is a default-deny: the agent reaches no
#: Daimon tool, `search_tools` and `call_tool` included, tools added later too.
#: Recognised only for the built-in server and only as an agent-wide `deny`.
WILDCARD_TOOL_KEY: Final = f"{DAIMON_SERVER_NAME}/*"

# The search transform's proxies: `call_tool` runs the middleware again with the
# inner tool's name.
SEARCH_PROXY_TOOLS: Final = frozenset({"call_tool", "search_tools"})


def _enforced_for(identity: AuthIdentity) -> bool:
    """A chat-vault credential for an agent: `chat_agent_id` set, `agent_id` not.

    Operator tokens, agent keys (`agent_id` claim) and credentials with no agent
    are not subject to an agent's rules.
    """
    return (
        identity.chat_agent_id is not None
        and identity.agent_id is None
        and not identity.is_operator
    )


async def tool_rule_refusal(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    identity: AuthIdentity,
    tool_name: str,
    policy: ToolSafetyPolicy | None,
    public_url: str | None,
) -> str | None:
    """Why this agent's rules refuse `tool_name`, or None when the call may go on.

    Mirrors the driver's gate: nothing is read unless `tool_safety.enabled` and
    `tool_safety.agent_rules` are both on, so a deployment that leaves rules
    inactive pays no query. Without a `public_url` there is no built-in server
    to name (the driver's `trusted_servers_for` is empty then too).

    A failed read refuses the call. The driver answers a failed rule read with
    an approval card in chat and a refusal when unattended; the server has no
    card, so it takes the refusal for both. Any DB failure here would break most
    tools anyway.
    """
    if (
        policy is None
        or not policy.enabled
        or not policy.agent_rules
        or not public_url
        or not _enforced_for(identity)
    ):
        return None
    chat_agent_id = identity.chat_agent_id
    assert chat_agent_id is not None  # _enforced_for
    key = f"{DAIMON_SERVER_NAME}/{tool_name}"
    # The search proxies are never refused by their own name, only by the wildcard.
    keys = [WILDCARD_TOOL_KEY] if tool_name in SEARCH_PROXY_TOOLS else [key, WILDCARD_TOOL_KEY]
    try:
        async with sessionmaker() as session:
            rows = await agent_tool_rules.deny_rules_for_trusted_tool(
                session, tenant_id=identity.tenant_id, tool_keys=keys
            )
    except Exception as err:
        log.warning(
            "tool_safety.server_rule_read_failed",
            tool=key,
            tenant=_prefix(identity.tenant_id),
            error=type(err).__name__,
        )
        return (
            f"Refused: could not check this agent's tool rules for {tool_name}. Nothing was done."
        )
    wildcard = False
    for row in rows:
        # Rows hold the agent's own daimon-mcp entry URL. A server that merely
        # wears the reserved name elsewhere is not this one.
        if not same_mcp_url(row.server_url, public_url):
            continue
        if (
            derive_agent_uuid(tenant_id=identity.tenant_id, ma_agent_id=row.agent_id)
            != chat_agent_id
        ):
            continue
        if row.tool_key != WILDCARD_TOOL_KEY:
            return _denied(identity, key, chat_agent_id, wildcard=False)
        wildcard = True
    if wildcard:
        return _denied(identity, key, chat_agent_id, wildcard=True)
    return None


def _denied(identity: AuthIdentity, key: str, agent: uuid.UUID, *, wildcard: bool) -> str:
    log.info(
        "tool_safety.server_rule_denied",
        tool=key,
        tenant=_prefix(identity.tenant_id),
        agent=str(agent),
        wildcard=wildcard,
    )
    # Only an admin sets an agent's tool rules (`Action.SET_AGENT_TOOL_RULE`), owner or not.
    admin = admin_noun(identity.platform)
    if wildcard:
        return (
            "Refused: a tool rule set on this agent blocks all Daimon tools. Nothing was "
            f"done. Only {admin} can change this agent's tool rules."
        )
    return (
        f"Refused: a tool rule set on this agent blocks {key.partition('/')[2]}. Nothing was "
        f"done. Only {admin} can change this agent's tool rules."
    )


def _prefix(tenant_id: uuid.UUID) -> str:
    return str(tenant_id)[:8]
