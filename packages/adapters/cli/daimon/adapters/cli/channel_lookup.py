"""The parent channel a channel or thread id names, for settings stored per channel.

Budgets and support plans both count against parent channels, so a value saved on
a thread id would never apply. One lookup for every command that stores one.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import typer
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.errors import DaimonError

DISCORD_API = "https://discord.com/api/v10"
_DISCORD_THREAD_TYPES = frozenset({10, 11, 12})
_DISCORD_MISSING = frozenset({403, 404})  # the bot cannot see the channel, or it is gone
_TEAMS_THREAD = ";messageid="


def split_thread(platform: str, value: str) -> str:
    """A thread id's channel part.

    Slack threads are `<channel>:<thread ts>`, Teams threads
    `<channel>;messageid=<root>`; a Teams channel id itself contains ":".
    """
    separator = _TEAMS_THREAD if platform == "teams" else ":"
    channel_id = value.strip().partition(separator)[0].strip()
    if not channel_id:
        raise typer.BadParameter("channel id must not be empty")
    return channel_id


async def fetch_discord_channel(
    token: str, *, channel_id: str, transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any] | None:
    """The channel as Discord returns it to the bot; None when the bot cannot see it."""
    try:
        async with httpx.AsyncClient(
            base_url=DISCORD_API,
            headers={"Authorization": f"Bot {token}"},
            timeout=10.0,
            transport=transport,
        ) as http:
            response = await http.get(f"/channels/{channel_id}")
    except httpx.HTTPError as exc:
        raise DaimonError(f"could not reach Discord to look up channel {channel_id}") from exc
    if response.status_code in _DISCORD_MISSING:
        return None
    if not response.is_success:
        raise DaimonError(
            f"Discord returned HTTP {response.status_code} looking up channel {channel_id}"
        )
    return cast("dict[str, Any]", response.json())


async def discord_parent_channel(
    rt: CliRuntime,
    *,
    guild_id: str,
    channel_id: str,
    transport: httpx.AsyncBaseTransport | None,
    missing_ok: bool = False,
) -> str:
    """The server channel a Discord id counts against: a thread's parent.

    The id is looked up with the bot token. With `missing_ok`, an id the bot
    cannot see (403 or 404), or any id when no bot token is set, is returned as
    given, so what was stored for a deleted channel can still be cleared.
    """
    if not channel_id.isdigit():
        raise typer.BadParameter(f"{channel_id!r} is not a Discord channel id")
    if rt.settings.discord is None:
        if missing_ok:
            return channel_id
        raise DaimonError(
            "DAIMON_DISCORD__BOT_TOKEN is not set; it is needed to look the channel up"
        )
    channel = await fetch_discord_channel(
        rt.settings.discord.bot_token.get_secret_value(), channel_id=channel_id, transport=transport
    )
    if channel is None:
        if missing_ok:
            return channel_id
        raise DaimonError(f"Discord channel {channel_id} is not visible to daimon")
    if str(channel.get("guild_id")) != guild_id:
        raise DaimonError(f"channel {channel_id} is not in server {guild_id}")
    if channel.get("type") in _DISCORD_THREAD_TYPES:
        return str(channel["parent_id"])
    return channel_id


async def parent_channel(
    rt: CliRuntime,
    *,
    platform: str,
    workspace_id: str,
    channel_id: str,
    transport: httpx.AsyncBaseTransport | None,
    missing_ok: bool = False,
) -> str:
    """The parent channel any platform's channel or thread id names."""
    target = split_thread(platform, channel_id)
    if platform != "discord":
        return target
    return await discord_parent_channel(
        rt, guild_id=workspace_id, channel_id=target, transport=transport, missing_ok=missing_ok
    )
