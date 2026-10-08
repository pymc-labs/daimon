"""Remember the names Teams hands us on messages and card clicks.

Every activity names its sender (`from.name`), and a channel message names its
channel (General's filled in by `identity.parse_inbound`). The `billing` card
falls back to what is stored here when a team roster or channel listing does
not answer. Recording is best-effort and in the background
(`daimon.core.platform_names`): it never delays or fails the message.
"""

from __future__ import annotations

import uuid

from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.platform_names import remember_channel_names, remember_user_name
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def remember_inbound(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, inbound: TeamsInbound
) -> None:
    """Store the sender's name and, for a channel message, the channel's."""
    remember_user_name(
        sessionmaker,
        tenant_id=tenant_id,
        platform="teams",
        user_id=inbound.user_id,
        display_name=inbound.user_name,
    )
    if inbound.kind == "channel" and inbound.channel_name:
        remember_channel_names(
            sessionmaker,
            tenant_id=tenant_id,
            platform="teams",
            names={inbound.channel_id: inbound.channel_name},
        )
