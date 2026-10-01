"""Whether a member may change what a pinned agent reaches, from where they are.

One rule for every path that adds to or edits an agent's configuration (keys,
connector tokens, MCP servers, prompt, tools, skills): an admin always may; on
an agent the operator pinned to channels, anyone else may only act from a
conversation inside those channels; an unpinned agent is unaffected. The
request tools, the direct configuration tools and the private forms' submit
paths all call `pin_write_refusal`, so the rule and its names can't drift.
"""

from __future__ import annotations

from collections.abc import Mapping

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import (
    TenantAccessPolicy,
    is_outside_agent_pin,
    origin_pin_location,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import CredentialRequestRow, Role
from sqlalchemy.ext.asyncio import AsyncSession

PIN_WRITE_REFUSAL = (
    "An operator pinned this agent to its own channels, so its keys, connections and "
    "configuration can only be changed from a conversation inside them, or by a server "
    "or workspace admin. Nothing was changed."
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


def pin_write_refused(
    policy: TenantAccessPolicy,
    *,
    is_admin: bool,
    agent_names: tuple[str | None, ...] | None,
    parent_channel_id: str | None,
    thread_id: str | None,
) -> bool:
    """True when this write must be refused.

    ``agent_names`` is None when the target could not be established (a legacy
    or vanished agent); under any pin that fails closed. ``parent_channel_id``
    and ``thread_id`` locate the conversation the write comes from; both None
    means there is none, which is outside every pin.
    """
    if is_admin or not policy.agent_channel_pins:
        return False
    if agent_names is None:
        return True
    channel_id, parent = origin_pin_location(
        parent_channel_id=parent_channel_id, thread_id=thread_id
    )
    return is_outside_agent_pin(
        policy, agent_names=agent_names, channel_id=channel_id, parent_channel_id=parent
    )


async def request_pin_refusal(
    session: AsyncSession, *, row: CredentialRequestRow, agent: BetaManagedAgentsAgent | None
) -> str | None:
    """Refusal copy when a private form may not be applied to its pinned target, else None.

    Called by every submit path just before the form is consumed, with the
    target resolved afresh by its stable id (``row.agent_id``), so a pin
    added since the card was posted, a rename, or a pin keyed by the config
    name all hold. An unreadable policy refuses. Admin is the requester's
    stored role, the signal the MCP gate reads too.
    """
    try:
        policy = await load_access_policy(session, tenant_id=row.tenant_id)
    except AccessPolicyUnreadable:
        return POLICY_UNREADABLE_REFUSAL
    if not policy.agent_channel_pins:
        return None
    account = await get_account(session, row.account_id)
    refused = pin_write_refused(
        policy,
        is_admin=account is not None and account.role is Role.ADMIN,
        agent_names=None if agent is None else agent_pin_names(agent.name, agent.metadata),
        parent_channel_id=row.parent_channel_id,
        thread_id=row.origin_thread_id,
    )
    return PIN_WRITE_REFUSAL if refused else None
