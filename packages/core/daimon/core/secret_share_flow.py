"""The steps every secret-share path takes: /share-secret, `share_secret_with` and both buttons.

A share is committed before its card is sent and deleted when the send fails, so no
transaction waits on Discord and no value waits with no card to reveal it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from daimon.core import secret_shares as words
from daimon.core.config import Settings, env_dm_policy
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores import secret_shares as store
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.secret_shares import ShareView
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Send = Callable[[ShareView], Awaitable[tuple[str, str]]]
"""Post a share's card; returns its (channel id, message id)."""


async def sharing_refusal(
    session: AsyncSession,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    recipient: str,
    agent_id: str | None,
) -> str | None:
    """Why a share to `recipient` may not be made or revealed now; None when it may."""
    place = FeaturePlace(tenant_id, "discord", agent_id=agent_id)
    if not (await feature_in(session, settings, place, "secret_sharing")).on:
        return words.OFF
    if not await dm_enabled(session, tenant_id=tenant_id):
        return words.DMS_OFF
    if not env_dm_policy(settings.direct_message_policies, tenant_id).allows(recipient):
        return words.DM_NOT_ALLOWED
    return None


async def post_card(
    sessions: async_sessionmaker[AsyncSession], share: ShareView, send: Send
) -> None:
    """Send the card and record it; a failed send deletes the share, then raises."""
    try:
        channel_id, message_id = await send(share)
    except BaseException:
        async with sessions.begin() as session:
            await store.delete_share(session, share.id)
        raise
    async with sessions.begin() as session:
        await store.set_share_card(session, share.id, channel_id=channel_id, message_id=message_id)


async def offer_share(
    sessions: async_sessionmaker[AsyncSession],
    send: Send,
    *,
    tenant_id: uuid.UUID,
    sender: str,
    recipient: str,
    label: str,
    value: str,
    now: datetime,
    agent_id: str | None = None,
    awaiting_sender: bool = False,
) -> ShareView | None:
    """Store a share and post its card; None when the sender has the most live ones."""
    async with sessions.begin() as session:
        share = await store.create_share(
            session,
            tenant_id=tenant_id,
            platform="discord",
            sender=sender,
            recipient=recipient,
            label=label,
            value=value,
            now=now,
            agent_id=agent_id,
            awaiting_sender=awaiting_sender,
        )
    if share is not None:
        await post_card(sessions, share, send)
    return share
