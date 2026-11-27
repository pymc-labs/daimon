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
and `ask` and channel rules need the driver. A provider turn has none, so its call
to a tool with one goes to the host call gate (`needs_host_gate`), which decides it
as the driver does. The chat credential is shared by chat turns and routines, and
the driver denies in both (a deny ignores `scope` and `attended`), so this does too.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
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


def _agent_of(identity: AuthIdentity, *, agent_keys: bool) -> uuid.UUID | None:
    """The agent whose rules apply: a chat-vault credential's (`chat_agent_id` set,
    `agent_id` not), and with `agent_keys` an agent key's (`agent_id` claim).

    Operator tokens and credentials with no agent are not subject to an agent's rules.
    """
    if identity.is_operator:
        return None
    if identity.agent_id is not None:
        return identity.agent_id if agent_keys else None
    return identity.chat_agent_id


async def tool_rule_refusal(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    identity: AuthIdentity,
    tool_name: str,
    policy: ToolSafetyPolicy | None,
    public_url: str | None,
    agent_keys: bool = False,
) -> str | None:
    """Why this agent's rules refuse `tool_name`, or None when the call may go on.
    `agent_keys` holds an agent key to its agent's rules too (a shared tool's call);
    a key has no card, so an `ask` rule refuses it as well.

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
        or (chat_agent_id := _agent_of(identity, agent_keys=agent_keys)) is None
    ):
        return None
    key = f"{DAIMON_SERVER_NAME}/{tool_name}"
    # The search proxies are never refused by their own name, only by the wildcard.
    keys = [WILDCARD_TOOL_KEY] if tool_name in SEARCH_PROXY_TOOLS else [key, WILDCARD_TOOL_KEY]
    try:
        async with sessionmaker() as session:
            rows = await agent_tool_rules.deny_rules_for_trusted_tool(
                session,
                tenant_id=identity.tenant_id,
                tool_keys=keys,
                rules=("deny", "ask") if agent_keys else ("deny",),
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
    wildcard = asked = False
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
        if row.rule == "ask":
            asked = True
        elif row.tool_key != WILDCARD_TOOL_KEY:
            return _denied(identity, key, chat_agent_id, wildcard=False)
        else:
            wildcard = True
    if wildcard:
        return _denied(identity, key, chat_agent_id, wildcard=True)
    if asked:
        log.info("tool_safety.server_rule_asked", tool=key, tenant=_prefix(identity.tenant_id))
        return (
            f"Refused: {tool_name} needs approval, which an agent key can't give. Nothing was done."
        )
    return None


async def needs_host_gate(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    identity: AuthIdentity,
    tool_name: str,
    policy: ToolSafetyPolicy | None,
    public_url: str | None,
) -> bool:
    """Whether this agent's `ask` or channel `deny` rule on `tool_name` sends a provider
    turn's call to the host call gate. A failed read sends it there too, where a failed
    rule read cards or refuses it as the driver does."""
    if (
        policy is None
        or not policy.enabled
        or not policy.agent_rules
        or not public_url
        or _agent_of(identity, agent_keys=False) is None
        or tool_name in SEARCH_PROXY_TOOLS
    ):
        return False
    try:
        async with sessionmaker() as session:
            rows = await agent_tool_rules.gate_rules_for_trusted_tool(
                session, tenant_id=identity.tenant_id, tool_key=f"{DAIMON_SERVER_NAME}/{tool_name}"
            )
    except Exception:  # fail closed: the gate re-reads and decides
        return True
    return any(
        same_mcp_url(row.server_url, public_url)
        and derive_agent_uuid(tenant_id=identity.tenant_id, ma_agent_id=row.agent_id)
        == identity.chat_agent_id
        for row in rows
    )


def _denied(identity: AuthIdentity, key: str, agent: uuid.UUID, *, wildcard: bool) -> str:
    log.info(
        "tool_safety.server_rule_denied",
        tool=key,
        tenant=_prefix(identity.tenant_id),
        agent=str(agent),
        wildcard=wildcard,
    )
    if wildcard:
        return "Refused: this agent's tool rules deny all Daimon tools. Nothing was done."
    return f"Refused: this agent's tool rule denies {key.partition('/')[2]}. Nothing was done."


def _prefix(tenant_id: uuid.UUID) -> str:
    return str(tenant_id)[:8]
