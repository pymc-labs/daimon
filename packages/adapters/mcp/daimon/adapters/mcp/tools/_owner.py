"""Whether the MCP caller is an owner of an agent (`daimon.core.stores.agent_owners`).

The primary owner and every co-owner count.

Only a person's chat credential can be an owner: an agent-scoped key, a
client requester from another organisation and a credential with no person
behind it never are. Owners are read at each check, so a change of owner
applies to the next call.
"""

from __future__ import annotations

from collections.abc import Collection

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_owners


def may_own(auth: AuthIdentity) -> bool:
    """A person's own credential, the only kind that can stand for an agent's owner."""
    return auth.agent_id is None and auth.platform_user_id is not None and not auth.client


async def caller_owns_agent(runtime: McpRuntime, auth: AuthIdentity, ma_agent_id: str) -> bool:
    if not may_own(auth):
        return False
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=ma_agent_id)
    async with runtime.session_factory() as session:
        return await agent_owners.is_owner(session, auth.tenant_id, agent_id, auth.account_id)


async def caller_owned_ids(runtime: McpRuntime, auth: AuthIdentity) -> frozenset[str]:
    """The provider ids of every agent the caller owns, matched by derived identity.

    Returned as a predicate input for listings: `derive_agent_uuid` of an MA id
    is compared against the owner rows, never the other way round.
    """
    if not may_own(auth):
        return frozenset()
    async with runtime.session_factory() as session:
        owned = await agent_owners.owned_by(session, auth.tenant_id, auth.account_id)
    return frozenset(str(agent_id) for agent_id in owned)


def owned_in(owned: frozenset[str], auth: AuthIdentity, ma_agent_id: str) -> bool:
    return str(derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=ma_agent_id)) in owned


#: An owner with no stored name on this platform: still someone to ask.
OWNER_UNNAMED = "another member"


async def owner_labels(
    runtime: McpRuntime, auth: AuthIdentity, ma_agent_ids: Collection[str]
) -> dict[str, str]:
    """Who owns each owned agent, said for the caller: "you", their name, or another member.

    Agents without an owner are left out (only an admin changes them). Names come
    from the stored names of people seen on the caller's platform; no lookup runs.
    """
    from daimon.core.stores.platform_names import get_user_names

    by_uuid = {
        derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=ma_agent_id): ma_agent_id
        for ma_agent_id in ma_agent_ids
    }
    platform = auth.platform or ""
    async with runtime.session_factory() as session:
        owners = await agent_owners.owner_identities(
            session, auth.tenant_id, by_uuid, platform=platform
        )
        users = {external for _, external in owners.values() if external is not None}
        names = await get_user_names(
            session, tenant_id=auth.tenant_id, platform=platform, user_ids=users
        )
    labels: dict[str, str] = {}
    for agent_id, (account_id, external) in owners.items():
        if may_own(auth) and account_id == auth.account_id:
            labels[by_uuid[agent_id]] = "you"
            continue
        name = names.get(external) if external is not None else None
        labels[by_uuid[agent_id]] = (name.label if name is not None else None) or OWNER_UNNAMED
    return labels
