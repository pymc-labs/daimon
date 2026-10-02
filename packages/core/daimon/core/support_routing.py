"""Who hears an ask-a-human request from a channel that has its own admins.

A channel with a channel admin grant sends its requests to those admins by DM
first, then to the server admins when none of them could be reached; the
deployment's escalation channel gets it only when neither did. A channel with
no grant keeps the escalation channel. Delivery is the adapter's, after the
request's ledger row is committed (`record_escalation*`), so routing never
changes what is spent. The DM carries the same fields as the escalation post
(the requester, a link to the answer and their note) and reaches only people
of the tenant the channel belongs to.
"""

from __future__ import annotations

import uuid
from typing import Final

from daimon.core.channel_admins import GroupMembers, channel_admin_user_ids
from daimon.core.stores.accounts import list_platform_user_ids
from sqlalchemy.ext.asyncio import AsyncSession

MAX_RECIPIENTS: Final = 10


async def support_recipient_tiers(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    requester_id: str,
    members: GroupMembers | None = None,
) -> tuple[tuple[str, ...], ...]:
    """The DM tiers to try in order (channel admins, then server admins); () for no grant.

    The requester is left out of both: asking for a human must reach someone else.
    `members` re-checks a channel admin matched by a stored Slack group or Teams team.
    """
    channel_admins = await channel_admin_user_ids(
        session,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        limit=MAX_RECIPIENTS,
        members=members,
    )
    if channel_admins is None:
        return ()
    server_admins = await list_platform_user_ids(
        session, tenant_id=tenant_id, platform=platform, limit=MAX_RECIPIENTS, admins=True
    )
    return tuple(
        tuple(uid for uid in tier if uid != requester_id)
        for tier in (channel_admins, server_admins)
    )


__all__ = ["MAX_RECIPIENTS", "support_recipient_tiers"]
