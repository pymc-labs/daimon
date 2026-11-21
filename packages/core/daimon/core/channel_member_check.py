"""Ask Discord or Slack whether one person can read one channel, for a run that reaches it.

A workflow run speaks for its run-as person with no chat turn behind it, so a channel whose
readers are limited is reached only when that person can read it on the platform. Discord:
the member's roles against the channel's overwrites, plus thread membership for a private
thread. Slack: the channel's member list, with the workspace's stored bot token. Teams has no
membership read here, so no channel. Any failure answers no (fail closed).
"""

from __future__ import annotations

import httpx
import structlog
from cryptography.fernet import MultiFernet
from daimon.core.channel_post_check import (
    DISCORD_API,
    SLACK_API,
    PlatformUnreadable,
    as_dict,
    discord_headers,
    discord_overwrites,
    discord_standing,
    response_json,
    slack_token,
)
from daimon.core.channel_visibility import discord_base_permissions, discord_can_view
from daimon.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_PRIVATE_THREAD = 12
_THREADS = frozenset({10, 11, 12})
_SLACK_PAGES = 20


async def _discord_reads(
    http: httpx.AsyncClient, settings: Settings, guild_id: str, user_id: str, channel_id: str
) -> bool:
    headers = discord_headers(settings)
    channel = as_dict(
        response_json(await http.get(f"{DISCORD_API}/channels/{channel_id}", headers=headers))
    )
    if str(channel.get("guild_id")) != guild_id:
        return False
    if channel.get("type") == _PRIVATE_THREAD:
        joined = await http.get(
            f"{DISCORD_API}/channels/{channel_id}/thread-members/{user_id}", headers=headers
        )
        if joined.status_code != 200:
            return False
    if channel.get("type") in _THREADS:
        parent = str(channel.get("parent_id"))
        channel = as_dict(
            response_json(await http.get(f"{DISCORD_API}/channels/{parent}", headers=headers))
        )
    member, roles = await discord_standing(http, headers, guild_id, user_id)
    return discord_can_view(
        member,
        guild_id=guild_id,
        base_permissions=discord_base_permissions(
            member, guild_id=guild_id, role_permissions=roles
        ),
        overwrites=discord_overwrites(channel.get("permission_overwrites")),
    )


async def _slack_reads(http: httpx.AsyncClient, token: str, user_id: str, channel_id: str) -> bool:
    cursor = ""
    for _ in range(_SLACK_PAGES):
        body = as_dict(
            response_json(
                await http.get(
                    f"{SLACK_API}/conversations.members",
                    params={
                        "channel": channel_id.split(":", 1)[0],
                        "limit": "1000",
                        "cursor": cursor,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
        )
        if body.get("ok") is not True:
            raise PlatformUnreadable("the member list was refused")
        members = body.get("members")
        if isinstance(members, list) and user_id in members:
            return True
        cursor = str(as_dict(body.get("response_metadata")).get("next_cursor") or "")
        if not cursor:
            return False
    raise PlatformUnreadable("too many pages")


async def fetch_member_reads(
    http: httpx.AsyncClient,
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet | None,
    platform: str,
    workspace_id: str,
    user_id: str,
    channel_id: str,
) -> bool:
    """Whether `user_id` can read `channel_id` (or a thread id) on `platform`; no when unsure."""
    try:
        if platform == "discord":
            return await _discord_reads(http, settings, workspace_id, user_id, channel_id)
        if platform == "slack":
            token = await slack_token(sessionmaker, fernet, workspace_id)
            return await _slack_reads(http, token, user_id, channel_id)
    except (PlatformUnreadable, httpx.HTTPError, ValueError, TypeError) as exc:
        log.info("workflow.member_read_unverified", platform=platform, error=str(exc))
    return False
