"""Which workspace channels a member can read on their chat platform, asked live.

Discord: the member's roles and every channel's overwrites over REST, with the
bot token this process holds. Slack: `users.conversations` with the workspace's
stored bot token, for the member and for the bot. Teams: no membership read here,
so no channel. Any failure hides every channel (fail closed). Answers are cached for
`CACHE_SECONDS`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import httpx
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.channel_visibility import (
    DiscordMember,
    DiscordOverwrite,
    discord_visible_channels,
)
from daimon.core.github_credentials import decrypt_token
from daimon.core.routine_destinations import discord_deliverable_channels, slack_may_deliver
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token

log = structlog.get_logger(__name__)

CACHE_SECONDS = 60.0
_DISCORD_API = "https://discord.com/api/v10"
_SLACK_API = "https://slack.com/api"
_SLACK_PAGES = 20
_DISCORD_TEXT_TYPES = frozenset({0, 5})
"""Text and announcement channels: the types chat accepts as a routine destination."""
_CACHE_SIZE = 2048


@dataclass(frozen=True)
class _Reach:
    """What one member can do in a workspace's channels, as the platform answered."""

    readable: frozenset[str]
    postable: frozenset[str]


_cache: dict[tuple[uuid.UUID, str], tuple[float, _Reach]] = {}


class _Unverified(Exception):
    """The platform's answer could not be read."""


def forget_visibility() -> None:
    """Drop every cached answer (tests)."""
    _cache.clear()


def _json(response: httpx.Response) -> object:
    if response.status_code != 200:
        raise _Unverified(f"status {response.status_code}")
    try:
        return cast(object, response.json())
    except ValueError as exc:
        raise _Unverified("not json") from exc


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _Unverified("expected an object")
    return cast("dict[str, object]", value)


def _dicts(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Unverified("expected a list")
    return [_dict(item) for item in cast("list[object]", value)]


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


def _overwrites(raw: object) -> tuple[DiscordOverwrite, ...]:
    return tuple(
        DiscordOverwrite(
            id=str(item.get("id")),
            type=_int(item.get("type")),
            allow=_int(item.get("allow")),
            deny=_int(item.get("deny")),
        )
        for item in _dicts(raw)
    )


async def _discord(runtime: WebRuntime, guild_id: str, user_id: str) -> _Reach:
    discord = runtime.settings.discord
    if discord is None:
        raise _Unverified("no bot token")
    headers = {"Authorization": f"Bot {discord.bot_token.get_secret_value()}"}
    get = runtime.http_client.get
    guild = _dict(_json(await get(f"{_DISCORD_API}/guilds/{guild_id}", headers=headers)))
    member = _dict(
        _json(await get(f"{_DISCORD_API}/guilds/{guild_id}/members/{user_id}", headers=headers))
    )
    channels = _dicts(
        _json(await get(f"{_DISCORD_API}/guilds/{guild_id}/channels", headers=headers))
    )
    roles = member.get("roles")
    overwrites = {
        str(row.get("id")): _overwrites(row.get("permission_overwrites")) for row in channels
    }
    member_facts = DiscordMember(
        user_id=user_id,
        role_ids=frozenset(str(r) for r in cast("list[object]", roles))
        if isinstance(roles, list)
        else frozenset(),
        is_owner=str(guild.get("owner_id")) == user_id,
        timed_out=_timed_out(member),
    )
    text_channels = [
        str(row.get("id")) for row in channels if row.get("type") in _DISCORD_TEXT_TYPES
    ]
    role_permissions = {
        str(role.get("id")): _int(role.get("permissions")) for role in _dicts(guild.get("roles"))
    }
    return _Reach(
        readable=discord_visible_channels(
            member_facts,
            guild_id=guild_id,
            role_permissions=role_permissions,
            channel_overwrites=overwrites,
            channel_ids=overwrites.keys(),
        ),
        postable=discord_deliverable_channels(
            member_facts,
            guild_id=guild_id,
            role_permissions=role_permissions,
            channel_overwrites=overwrites,
            channel_ids=text_channels,
        ),
    )


async def _slack_token(runtime: WebRuntime, team_id: str) -> str:
    async with runtime.sessionmaker() as db:
        row = await get_slack_bot_token(db, team_id=team_id)
    if row is None:
        raise _Unverified("no bot token")
    try:
        return decrypt_token(runtime.fernet, row.encrypted_token)
    except InvalidToken as exc:
        raise _Unverified("bad token") from exc


async def _slack_conversations(
    runtime: WebRuntime, token: str, user_id: str | None
) -> list[dict[str, object]]:
    """The channels `user_id` is in; with no user, the channels the bot is in. One call per page."""
    found: list[dict[str, object]] = []
    cursor = ""
    for _ in range(_SLACK_PAGES):
        params = {
            "types": "public_channel,private_channel",
            "exclude_archived": "true",
            "limit": "1000",
            "cursor": cursor,
        }
        if user_id is not None:
            params["user"] = user_id
        body = _dict(
            _json(
                await runtime.http_client.get(
                    f"{_SLACK_API}/users.conversations",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
        )
        if body.get("ok") is not True:
            raise _Unverified("slack refused")
        found.extend(_dicts(body.get("channels")))
        meta = body.get("response_metadata")
        cursor = str(_dict(meta).get("next_cursor") or "") if meta is not None else ""
        if not cursor:
            return found
    raise _Unverified("too many pages")


async def _slack_guest(runtime: WebRuntime, token: str, user_id: str) -> bool:
    """Whether the member is a guest, as chat's `check_channel_access` reads it."""
    body = _dict(
        _json(
            await runtime.http_client.get(
                f"{_SLACK_API}/users.info",
                params={"user": user_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
    )
    if body.get("ok") is not True:
        raise _Unverified("slack refused")
    user = _dict(body.get("user"))
    return bool(user.get("is_restricted") or user.get("is_ultra_restricted"))


async def _slack(runtime: WebRuntime, team_id: str, user_id: str) -> _Reach:
    token = await _slack_token(runtime, team_id)
    mine = frozenset(str(c.get("id")) for c in await _slack_conversations(runtime, token, user_id))
    in_bot = await _slack_conversations(runtime, token, None)
    is_guest = await _slack_guest(runtime, token, user_id)
    # Candidates are the channels daimon is in (the listing leaves out archived ones).
    # The member's own membership counts only where the rule needs it.
    return _Reach(
        readable=mine,
        postable=frozenset(
            str(channel.get("id"))
            for channel in in_bot
            if slack_may_deliver(
                bot_is_member=True,
                is_archived=False,
                is_im_or_mpim=False,
                is_private=bool(channel.get("is_private")),
                is_guest=is_guest,
                caller_is_member=str(channel.get("id")) in mine,
            )
        ),
    )


async def _reach(
    runtime: WebRuntime,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    workspace_id: str,
    user_id: str | None,
) -> _Reach:
    """The member's reach in the workspace; nothing when unverified. Cached briefly."""
    none = _Reach(frozenset(), frozenset())
    if user_id is None or not workspace_id:
        return none
    key = (tenant_id, user_id)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        if platform == "discord":
            reach = await _discord(runtime, workspace_id, user_id)
        elif platform == "slack":
            reach = await _slack(runtime, workspace_id, user_id)
        else:
            reach = none
    except (_Unverified, httpx.HTTPError, ValueError, TypeError) as exc:
        log.info("web.channel_visibility_unverified", platform=platform, error=str(exc))
        return none
    if len(_cache) >= _CACHE_SIZE:
        _cache.clear()
    _cache[key] = (now, reach)
    return reach


async def readable_channels(
    runtime: WebRuntime,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    workspace_id: str,
    user_id: str | None,
    channel_ids: Collection[str],
) -> frozenset[str]:
    """The ids among `channel_ids` the member can read on `platform`; none when unverified.

    `user_id` is the member's id on the workspace's platform; None (no linked chat
    account) sees none.
    """
    reach = await _reach(
        runtime, tenant_id=tenant_id, platform=platform, workspace_id=workspace_id, user_id=user_id
    )
    return reach.readable & frozenset(channel_ids)


async def postable_channels(
    runtime: WebRuntime,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    workspace_id: str,
    user_id: str | None,
    channel_ids: Collection[str],
) -> frozenset[str]:
    """The ids among `channel_ids` a routine may deliver to for the member; none when unverified.

    The member must be able to read and post there (Discord) or be in the channel
    with daimon in it (Slack): the checks the chat tool makes when it creates a routine.
    """
    reach = await _reach(
        runtime, tenant_id=tenant_id, platform=platform, workspace_id=workspace_id, user_id=user_id
    )
    return reach.postable & frozenset(channel_ids)
