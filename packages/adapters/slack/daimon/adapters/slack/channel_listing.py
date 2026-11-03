"""List each workspace's channels the bot can see into `platform_channel_names`.

Slack's `conversations.list` is a tier 2 method (about 20 calls a minute), so pages
are spaced out; a round runs every few hours (`daimon.core.channel_listing`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from daimon.core.channel_listing import store_listing
from daimon.core.stores.platform_names import ChannelSighting
from daimon.core.stores.tenants import list_tenants_by_platform
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Seconds between pages: well under tier 2's limit.
PAGE_GAP_S = 3.5


async def list_workspace(
    client: AsyncWebClient, *, page_gap_s: float = PAGE_GAP_S
) -> dict[str, ChannelSighting]:
    """Every public channel, and private one the bot is in, with whether it is archived."""
    found: dict[str, ChannelSighting] = {}
    cursor: str | None = None
    while True:
        resp = await client.conversations_list(  # pyright: ignore[reportUnknownMemberType]
            types="public_channel,private_channel",
            exclude_archived=False,
            limit=200,
            cursor=cursor,
        )
        data = cast(dict[str, Any], resp.data)
        for ch in cast(list[dict[str, Any]], data.get("channels") or []):
            if ch.get("is_private") and not ch.get("is_member"):
                continue
            found[str(ch["id"])] = ChannelSighting(
                str(ch.get("name") or ch["id"]),
                kind="private" if ch.get("is_private") else "public",
                archived=bool(ch.get("is_archived")),
            )
        meta = cast(dict[str, Any], data.get("response_metadata") or {})
        cursor = cast(str | None, meta.get("next_cursor")) or None
        if cursor is None:
            return found
        await asyncio.sleep(page_gap_s)


async def list_all_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    client_for: Callable[[str], Awaitable[AsyncWebClient | None]],
    *,
    page_gap_s: float = PAGE_GAP_S,
) -> int:
    """One round: every Slack workspace with a bot token, one after another."""
    stored = 0
    for tenant in await list_tenants_by_platform(sessionmaker, platform="slack"):
        client = await client_for(tenant.external_id)
        if client is None:
            continue
        stored += await store_listing(
            sessionmaker,
            tenant_id=tenant.id,
            platform="slack",
            channels=await list_workspace(client, page_gap_s=page_gap_s),
        )
    return stored
