"""Whether a member may change what a pinned agent reaches, from where they are.

One rule for every path that adds to or edits an agent's configuration (keys,
connector tokens, MCP servers, prompt, tools, skills): an admin always may, and
so does a channel admin of every channel the agent is pinned to; on an agent
the operator pinned to channels, anyone else may only act from a conversation
inside those channels; an unpinned agent is unaffected. The
request tools, the direct configuration tools and the private forms' submit
paths all decide it with `daimon.core.authz.authorize(CONFIGURE)`, so the rule
and its names can't drift.
"""

from __future__ import annotations

from collections.abc import Mapping

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    Surface,
    agent_names,
    authorize,
    build_agent_ref,
)
from daimon.core.channel_admins import load_stored_subject
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import CredentialRequestRow
from sqlalchemy.ext.asyncio import AsyncSession

PIN_WRITE_REFUSAL = (
    "An operator pinned this agent to its own channels, so its keys, connections and "
    "configuration can only be changed from a conversation inside them, by an admin of "
    "all of them, or by a server or workspace admin. Nothing was changed."
)
POLICY_UNREADABLE_REFUSAL = (
    "This workspace's access policy could not be read, so nothing was changed. "
    "Ask an admin to check it."
)


def agent_pin_names(name: str | None, metadata: Mapping[str, str]) -> tuple[str | None, ...]:
    """Every name a pin on an agent may be keyed by (`daimon.core.authz.agent_names`)."""
    return agent_names(name, metadata)


async def request_pin_refusal(
    session: AsyncSession, *, row: CredentialRequestRow, agent: BetaManagedAgentsAgent | None
) -> str | None:
    """Refusal copy when a private form may not be applied to its pinned target, else None.

    Called by every submit path just before the form is consumed, with the
    target resolved afresh by its stable id (``row.agent_id``), so a pin
    added since the card was posted, a rename, or a pin keyed by the config
    name all hold. An unreadable policy refuses. Admin is the requester's
    stored role, the signal the MCP gate reads too; so are the requester's
    channel admin grants, matched against the roles stored at their last turn.
    """
    try:
        policy = await load_access_policy(session, tenant_id=row.tenant_id)
    except AccessPolicyUnreadable:
        return POLICY_UNREADABLE_REFUSAL
    if not policy.agent_channel_pins:
        return None
    decision = authorize(
        policy,
        subject=await load_stored_subject(
            session,
            tenant_id=row.tenant_id,
            platform=row.platform,
            account_id=row.account_id,
            platform_user_id=row.requester_platform_user_id,
        ),
        action=Action.CONFIGURE,
        surface=Surface.CONFIG,
        agent=(
            AgentRef.unresolved() if agent is None else build_agent_ref(agent.name, agent.metadata)
        ),
        place=Place.from_origin(
            parent_channel_id=row.parent_channel_id, thread_id=row.origin_thread_id
        ),
    )
    return None if decision else PIN_WRITE_REFUSAL
