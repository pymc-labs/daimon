"""Ask Discord or Slack whether Daimon can post in one channel, for every surface that picks one.

The web's support and alert pickers, the CLI and MCP all call `fetch_post_check`; the rules
are `support_channel_check`'s. Discord is read with the bot token in `Settings`, Slack with the
workspace's stored bot token. A failure is `unverified`, never `ready`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, cast

import httpx
import structlog
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.channel_visibility import (
    DiscordMember,
    DiscordOverwrite,
    discord_base_permissions,
    discord_channel_permissions,
)
from daimon.core.config import Settings
from daimon.core.github_credentials import decrypt_token
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.support_channel_check import (
    READY,
    UNVERIFIED,
    ChannelCheck,
    discord_check,
    discord_missing,
    discord_not_text,
    slack_check,
    teams_check,
)
from daimon.core.support_routing import SupportDestination
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

DISCORD_API: Final = "https://discord.com/api/v10"
SLACK_API: Final = "https://slack.com/api"
_DISCORD_TEXT: Final = 0
"""The one type Daimon posts requests in; announcement channels are refused as at post time."""


class PlatformUnreadable(Exception):
    """The platform's answer could not be read."""


def response_json(response: httpx.Response) -> object:
    if response.status_code != 200:
        raise PlatformUnreadable(f"status {response.status_code}")
    try:
        return cast(object, response.json())
    except ValueError as exc:
        raise PlatformUnreadable("not json") from exc


def as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PlatformUnreadable("expected an object")
    return cast("dict[str, object]", value)


def as_dicts(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlatformUnreadable("expected a list")
    return [as_dict(item) for item in cast("list[object]", value)]


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, str)) else 0


def _timed_out(member: dict[str, object]) -> bool:
    """Whether the member is under a communication timeout now."""
    until = member.get("communication_disabled_until")
    if not isinstance(until, str):
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now(UTC)
    except ValueError:
        return True


def discord_overwrites(raw: object) -> tuple[DiscordOverwrite, ...]:
    return tuple(
        DiscordOverwrite(
            id=str(item.get("id")),
            type=_int(item.get("type")),
            allow=_int(item.get("allow")),
            deny=_int(item.get("deny")),
        )
        for item in as_dicts(raw)
    )


def discord_headers(settings: Settings) -> dict[str, str]:
    if settings.discord is None:
        raise PlatformUnreadable("no bot token")
    return {"Authorization": f"Bot {settings.discord.bot_token.get_secret_value()}"}


async def discord_standing(
    http: httpx.AsyncClient, headers: dict[str, str], guild_id: str, user_id: str
) -> tuple[DiscordMember, dict[str, int]]:
    """The user's roles in the guild, and every role's permissions (the guild's id is @everyone)."""
    guild = as_dict(
        response_json(await http.get(f"{DISCORD_API}/guilds/{guild_id}", headers=headers))
    )
    member = as_dict(
        response_json(
            await http.get(f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}", headers=headers)
        )
    )
    roles = member.get("roles")
    facts = DiscordMember(
        user_id=user_id,
        role_ids=frozenset(str(r) for r in cast("list[object]", roles))
        if isinstance(roles, list)
        else frozenset(),
        is_owner=str(guild.get("owner_id")) == user_id,
        timed_out=_timed_out(member),
    )
    role_permissions = {
        str(role.get("id")): _int(role.get("permissions")) for role in as_dicts(guild.get("roles"))
    }
    return facts, role_permissions


async def slack_token(
    sessionmaker: async_sessionmaker[AsyncSession], fernet: MultiFernet | None, team_id: str
) -> str:
    async with sessionmaker() as db:
        row = await get_slack_bot_token(db, team_id=team_id)
    if row is None or fernet is None:
        raise PlatformUnreadable("no bot token")
    try:
        return decrypt_token(fernet, row.encrypted_token)
    except InvalidToken as exc:
        raise PlatformUnreadable("bad token") from exc


async def _discord_post_check(
    http: httpx.AsyncClient, settings: Settings, channel_id: str, label: str
) -> ChannelCheck:
    """Daimon's own permissions in the channel; the channel's guild is read from the channel."""
    headers = discord_headers(settings)
    bot = as_dict(response_json(await http.get(f"{DISCORD_API}/users/@me", headers=headers)))
    response = await http.get(f"{DISCORD_API}/channels/{channel_id}", headers=headers)
    if response.status_code in (403, 404):
        # Unknown, or Missing Access: either way Daimon cannot see it.
        return discord_missing(label)
    channel = as_dict(response_json(response))
    guild_id = channel.get("guild_id")
    if channel.get("type") != _DISCORD_TEXT or not isinstance(guild_id, str):
        return discord_not_text(label)
    member, role_permissions = await discord_standing(http, headers, guild_id, str(bot.get("id")))
    bits = discord_channel_permissions(
        member,
        guild_id=guild_id,
        base_permissions=discord_base_permissions(
            member, guild_id=guild_id, role_permissions=role_permissions
        ),
        overwrites=discord_overwrites(channel.get("permission_overwrites")),
    )
    return discord_check(bits=bits, label=label)


async def _slack_post_check(
    http: httpx.AsyncClient, token: str, channel_id: str, label: str
) -> ChannelCheck:
    """Slack's own record of the channel, with the workspace's bot token."""
    body = as_dict(
        response_json(
            await http.get(
                f"{SLACK_API}/conversations.info",
                params={"channel": channel_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
    )
    if body.get("ok") is not True:
        if body.get("error") == "channel_not_found":
            # A private channel Daimon is not in answers the same way as a missing one.
            return slack_check(label=label, found=False)
        raise PlatformUnreadable("slack refused")
    channel = as_dict(body.get("channel"))
    return slack_check(
        label=label,
        found=True,
        is_archived=bool(channel.get("is_archived")),
        is_private=bool(channel.get("is_private")),
        bot_is_member=bool(channel.get("is_member")),
    )


async def fetch_post_check(
    http: httpx.AsyncClient,
    destination: SupportDestination,
    *,
    label: str,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet | None,
) -> ChannelCheck:
    """Whether Daimon can post in `destination`; `unverified` when the platform won't say.

    `label` names the channel in the instruction ("#general").
    """
    if destination.platform == "teams":
        if not destination.own:
            return READY
        return teams_check(
            label=label,
            team_id=destination.teams_team_id,
            service_url=destination.teams_service_url,
        )
    try:
        if destination.platform == "discord":
            return await _discord_post_check(http, settings, destination.channel_id, label)
        if destination.platform == "slack" and destination.slack_team_id is not None:
            token = await slack_token(sessionmaker, fernet, destination.slack_team_id)
            return await _slack_post_check(http, token, destination.channel_id, label)
    except (PlatformUnreadable, httpx.HTTPError, ValueError, TypeError) as exc:
        log.info("support.channel_unverified", platform=destination.platform, error=str(exc))
    return UNVERIFIED


async def alert_channel_problem(
    http: httpx.AsyncClient,
    *,
    platform: str,
    workspace_id: str,
    channel_id: str,
    label: str,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet | None,
) -> str | None:
    """Why a budget's alerts cannot post to `channel_id`, or None; only a definite answer refuses.

    Teams has no answer without the channel's team pick, so its alert post checks instead.
    """
    if platform == "teams":
        return None
    destination = SupportDestination(
        platform=platform,
        channel_id=channel_id,
        own=True,
        slack_team_id=workspace_id if platform == "slack" else None,
    )
    check = await fetch_post_check(
        http, destination, label=label, settings=settings, sessionmaker=sessionmaker, fernet=fernet
    )
    return (check.detail or "Daimon can't post there.") if check.state == "blocked" else None
