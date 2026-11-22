"""The `bot_access_wait` feature: work aimed at a channel Daimon cannot reach waits quietly.

With it on, a routine, timer or watch whose Discord channel the bot cannot see or post in
is not failed, paused, counted as an error or sent to anyone by DM. It is held back, and
runs once the bot is given access. Only Discord's "Missing Access" (or missing permissions)
for the bot itself counts: a channel that is gone, in another server or not a text channel
keeps its old handling, and so does a person losing their own permission.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Final

import httpx
from daimon.core.channel_post_check import (
    DISCORD_API,
    PlatformUnreadable,
    as_dict,
    discord_headers,
    discord_overwrites,
    discord_standing,
    response_json,
)
from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_SEND_MESSAGES,
    DISCORD_SEND_MESSAGES_IN_THREADS,
    DISCORD_VIEW_CHANNEL,
    discord_base_permissions,
    discord_channel_permissions,
)
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOT_ACCESS_UNAVAILABLE: Final = "bot_access_unavailable"
"""The reason recorded on a routine's last error while it waits."""

BOT_ACCESS_RETRY_DELAY: Final = timedelta(minutes=15)
"""How long a wake waits before it looks again; gentler than the 30 s of a busy thread."""

_MISSING_ACCESS: Final = (50001, 50013)
"""Discord's "Missing Access" and "Missing Permissions" error codes."""
_TEXT_CHANNELS: Final = (0, 5)
_THREADS: Final = (10, 11, 12)
_THREAD_PARENTS: Final = (0, 5, 15)


class BotAccessUnavailable(Exception):
    """The bot cannot see or post in the channel; the work waits and is offered again."""


def is_missing_access(status: object, code: object) -> bool:
    """Whether a Discord answer (an HTTP status and error code) is the bot being locked out.

    A 404 (gone), an unknown type or any other failure is not: those keep their old handling.
    """
    return status == 403 and code in _MISSING_ACCESS


async def bot_access_wait_on(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, platform: str
) -> bool:
    """Whether the workspace waits for the bot's access; a failed read keeps it off."""
    found = await place_feature_or_none(
        sessionmaker, None, FeaturePlace.of(tenant_id, platform, None), "bot_access_wait"
    )
    return found is not None and found.value == "on"


async def _channel(
    http: httpx.AsyncClient, headers: dict[str, str], channel_id: str
) -> dict[str, object] | None:
    """The channel as the bot reads it; `None` when Discord says the bot is locked out."""
    response = await http.get(f"{DISCORD_API}/channels/{channel_id}", headers=headers)
    if response.status_code == 403:
        body = as_dict(response.json())
        if is_missing_access(403, body.get("code")):
            return None
    return as_dict(response_json(response))


async def _bot_blocked(
    http: httpx.AsyncClient, settings: Settings, guild_id: str, destination_id: str
) -> bool:
    headers = discord_headers(settings)
    channel = await _channel(http, headers, destination_id)
    if channel is None:
        return True
    if channel.get("guild_id") != guild_id:
        return False
    is_thread = channel.get("type") in _THREADS
    if not is_thread and channel.get("type") not in _TEXT_CHANNELS:
        return False
    source = channel
    if is_thread:
        parent_id = channel.get("parent_id")
        if not isinstance(parent_id, str):
            return False
        parent = await _channel(http, headers, parent_id)
        if parent is None:
            return True
        if parent.get("type") not in _THREAD_PARENTS:
            return False
        source = parent
    bot = as_dict(response_json(await http.get(f"{DISCORD_API}/users/@me", headers=headers)))
    member, role_permissions = await discord_standing(http, headers, guild_id, str(bot["id"]))
    bits = discord_channel_permissions(
        member,
        guild_id=guild_id,
        base_permissions=discord_base_permissions(
            member, guild_id=guild_id, role_permissions=role_permissions
        ),
        overwrites=discord_overwrites(source.get("permission_overwrites")),
    )
    if bits & DISCORD_ADMINISTRATOR:
        return False
    needs = DISCORD_VIEW_CHANNEL | (
        DISCORD_SEND_MESSAGES_IN_THREADS if is_thread else DISCORD_SEND_MESSAGES
    )
    return bits & needs != needs


async def bot_access_blocked(
    http: httpx.AsyncClient, settings: Settings, *, guild_id: str, destination_id: str
) -> bool:
    """True only when Discord says the bot cannot see or post in the routine's destination.

    The destination itself is read with the bot token, a thread through its parent: the
    bot needs View Channel and Send Messages (Send Messages in Threads for a thread).
    Embed Links is not needed. A channel in another server, a type delivery does not post
    in, a 404 and a check that cannot be read are not a block: they keep their old handling.
    """
    try:
        return await _bot_blocked(http, settings, guild_id, destination_id)
    except (PlatformUnreadable, httpx.HTTPError, ValueError, KeyError, TypeError):
        return False
