"""Which workspace channels a member can read on their chat platform, asked live.

Discord: the member's roles and every channel's overwrites over REST, with the
bot token this process holds. Slack: `users.conversations` with the workspace's
stored bot token. Teams: no membership read here, so no channel. Any failure
hides every channel (fail closed). Answers are cached for `CACHE_SECONDS`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Collection
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
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token

log = structlog.get_logger(__name__)

CACHE_SECONDS = 60.0
_DISCORD_API = "https://discord.com/api/v10"
_SLACK_API = "https://slack.com/api"
_SLACK_PAGES = 20
_CACHE_SIZE = 2048

_cache: dict[tuple[uuid.UUID, str], tuple[float, frozenset[str]]] = {}


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


async def _discord(runtime: WebRuntime, guild_id: str, user_id: str) -> frozenset[str]:
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
    return discord_visible_channels(
        DiscordMember(
            user_id=user_id,
            role_ids=frozenset(str(r) for r in cast("list[object]", roles))
            if isinstance(roles, list)
            else frozenset(),
            is_owner=str(guild.get("owner_id")) == user_id,
        ),
        guild_id=guild_id,
        role_permissions={
            str(role.get("id")): _int(role.get("permissions"))
            for role in _dicts(guild.get("roles"))
        },
        channel_overwrites=overwrites,
        channel_ids=overwrites.keys(),
    )


async def _slack(runtime: WebRuntime, team_id: str, user_id: str) -> frozenset[str]:
    async with runtime.sessionmaker() as db:
        row = await get_slack_bot_token(db, team_id=team_id)
    if row is None:
        raise _Unverified("no bot token")
    try:
        token = decrypt_token(runtime.fernet, row.encrypted_token)
    except InvalidToken as exc:
        raise _Unverified("bad token") from exc
    found: set[str] = set()
    cursor = ""
    for _ in range(_SLACK_PAGES):
        params = {
            "user": user_id,
            "types": "public_channel,private_channel",
            "exclude_archived": "true",
            "limit": "1000",
            "cursor": cursor,
        }
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
        found.update(str(channel.get("id")) for channel in _dicts(body.get("channels")))
        meta = body.get("response_metadata")
        cursor = str(_dict(meta).get("next_cursor") or "") if meta is not None else ""
        if not cursor:
            return frozenset(found)
    raise _Unverified("too many pages")


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
    if user_id is None or not workspace_id:
        return frozenset()
    key = (tenant_id, user_id)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        return cached[1] & frozenset(channel_ids)
    try:
        if platform == "discord":
            visible = await _discord(runtime, workspace_id, user_id)
        elif platform == "slack":
            visible = await _slack(runtime, workspace_id, user_id)
        else:
            visible = frozenset[str]()
    except (_Unverified, httpx.HTTPError, ValueError, TypeError) as exc:
        log.info("web.channel_visibility_unverified", platform=platform, error=str(exc))
        return frozenset()
    if len(_cache) >= _CACHE_SIZE:
        _cache.clear()
    _cache[key] = (now, visible)
    return visible & frozenset(channel_ids)
