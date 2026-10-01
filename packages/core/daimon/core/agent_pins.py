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

from collections.abc import Collection, Mapping

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.channel_admins import ChannelAdminCaller, load_administered_channel_ids
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import CredentialRequestRow, Role
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
    """Every name a pin on an agent may be keyed by: its MA name and its config name.

    Admission checks the same pair, so a pin on either holds everywhere.
    """
    return (name, metadata.get(MA_METADATA_KEY_NAME))


def is_pin_administered(
    policy: TenantAccessPolicy,
    *,
    agent_names: tuple[str | None, ...],
    administered_channel_ids: Collection[str],
) -> bool:
    """Whether the agent is pinned and every channel of every pin on it is administered.

    A pin to no channel at all is nobody's: only a server admin edits that agent.
    """
    pins = [
        policy.agent_channel_pins[name]
        for name in agent_names
        if name is not None and name in policy.agent_channel_pins
    ]
    return bool(pins) and all(
        pin and frozenset(pin) <= frozenset(administered_channel_ids) for pin in pins
    )


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
    account = await get_account(session, row.account_id)
    is_admin = account is not None and account.role is Role.ADMIN
    names = None if agent is None else agent_pin_names(agent.name, agent.metadata)
    administered: frozenset[str] = frozenset()
    if account is not None and not is_admin and names is not None:
        administered = await load_administered_channel_ids(
            session,
            tenant_id=row.tenant_id,
            platform=row.platform or "",
            caller=ChannelAdminCaller(
                platform_user_id=row.requester_platform_user_id,
                role_ids=frozenset(account.platform_role_ids),
            ),
        )
    if names is not None and is_pin_administered(
        policy, agent_names=names, administered_channel_ids=administered
    ):
        return None
    decision = authorize(
        policy,
        subject=Subject(is_admin=is_admin),
        action=Action.CONFIGURE,
        surface=Surface.CONFIG,
        agent=AgentRef.unresolved() if names is None else AgentRef.of(*names),
        place=Place.from_origin(
            parent_channel_id=row.parent_channel_id, thread_id=row.origin_thread_id
        ),
    )
    return None if decision else PIN_WRITE_REFUSAL
