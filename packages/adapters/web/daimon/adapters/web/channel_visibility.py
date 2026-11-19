"""Which workspace channels a member can read on their chat platform, asked live.

Discord: the member's roles and every channel's overwrites over REST, with the
bot token this process holds. Slack: `users.conversations` with the workspace's
stored bot token, for the member and for the bot. Teams: no membership read here,
so no channel. Any failure hides every channel (fail closed). Answers are cached for
`CACHE_SECONDS`.

`request_channel_check` asks the same platforms whether Daimon itself can post in one
channel (where support requests go); a failure there is `unverified`, never `ready`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass

import httpx
import structlog
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.channel_post_check import (
    DISCORD_API,
    SLACK_API,
    PlatformUnreadable,
    as_dict,
    as_dicts,
    discord_headers,
    discord_overwrites,
    discord_standing,
    fetch_post_check,
    response_json,
    slack_token,
)
from daimon.core.channel_visibility import (
    discord_visible_channels,
)
from daimon.core.routine_destinations import discord_deliverable_channels, slack_may_deliver
from daimon.core.support_channel_check import (
    ChannelCheck,
)
from daimon.core.support_routing import SupportDestination

log = structlog.get_logger(__name__)

CACHE_SECONDS = 60.0
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
_post_cache: dict[tuple[str, str, str], tuple[float, ChannelCheck]] = {}


def forget_visibility() -> None:
    """Drop every cached answer (tests)."""
    _cache.clear()
    _post_cache.clear()


async def _discord(runtime: WebRuntime, guild_id: str, user_id: str) -> _Reach:
    headers = discord_headers(runtime.settings)
    member_facts, role_permissions = await discord_standing(
        runtime.http_client, headers, guild_id, user_id
    )
    channels = as_dicts(
        response_json(
            await runtime.http_client.get(
                f"{DISCORD_API}/guilds/{guild_id}/channels", headers=headers
            )
        )
    )
    overwrites = {
        str(row.get("id")): discord_overwrites(row.get("permission_overwrites")) for row in channels
    }
    text_channels = [
        str(row.get("id")) for row in channels if row.get("type") in _DISCORD_TEXT_TYPES
    ]
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
        body = as_dict(
            response_json(
                await runtime.http_client.get(
                    f"{SLACK_API}/users.conversations",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
        )
        if body.get("ok") is not True:
            raise PlatformUnreadable("slack refused")
        found.extend(as_dicts(body.get("channels")))
        meta = body.get("response_metadata")
        cursor = str(as_dict(meta).get("next_cursor") or "") if meta is not None else ""
        if not cursor:
            return found
    raise PlatformUnreadable("too many pages")


async def _slack_guest(runtime: WebRuntime, token: str, user_id: str) -> bool:
    """Whether the member is a guest, as chat's `check_channel_access` reads it."""
    body = as_dict(
        response_json(
            await runtime.http_client.get(
                f"{SLACK_API}/users.info",
                params={"user": user_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
    )
    if body.get("ok") is not True:
        raise PlatformUnreadable("slack refused")
    user = as_dict(body.get("user"))
    return bool(user.get("is_restricted") or user.get("is_ultra_restricted"))


async def _slack(runtime: WebRuntime, team_id: str, user_id: str) -> _Reach:
    token = await slack_token(runtime.sessionmaker, runtime.fernet, team_id)
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
    except (PlatformUnreadable, httpx.HTTPError, ValueError, TypeError) as exc:
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


async def request_channel_check(
    runtime: WebRuntime, destination: SupportDestination, *, label: str, fresh: bool = False
) -> ChannelCheck:
    """`fetch_post_check`, cached for `CACHE_SECONDS`; `fresh` (a save) asks again."""
    key = (destination.platform, destination.slack_team_id or "", destination.channel_id)
    cached = _post_cache.get(key)
    now = time.monotonic()
    if not fresh and cached is not None and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    check = await fetch_post_check(
        runtime.http_client,
        destination,
        label=label,
        settings=runtime.settings,
        sessionmaker=runtime.sessionmaker,
        fernet=runtime.fernet,
    )
    if check.state != "unverified" and destination.platform != "teams":
        if len(_post_cache) >= _CACHE_SIZE:
            _post_cache.clear()
        _post_cache[key] = (now, check)
    return check
