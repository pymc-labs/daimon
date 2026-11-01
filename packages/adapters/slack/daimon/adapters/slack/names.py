"""Remember the names Slack hands us on events, slash commands and clicks.

A message event from a person often carries `user_profile` (display and real
name); a slash command and an interaction carry the username. `/billing`
falls back to what is stored here when `users.info` does not answer in time.
Recording is best-effort and in the background (`daimon.core.platform_names`):
it never delays or fails the event.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import remember_channel_names, remember_user_name
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.slack.runtime import SlackRuntime

#: How long a channel looked up once is left alone; a rename reaches us on the next lookup.
_LOOKUP_TTL_S = 6 * 3600
#: (team id, channel id) -> when it was last looked up, so a busy channel is asked once.
_looked_up: dict[tuple[str, str], float] = {}
_LOOKUP_MEMO_SIZE = 5_000


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def payload_name(kind: str, payload: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    """(team id, user id, display name, handle) a Socket Mode payload names, or blanks."""
    if kind == "events_api":
        event = _dict(payload.get("event"))
        profile = _dict(event.get("user_profile"))
        team = _text(payload.get("team_id")) or _text(event.get("team")) or ""
        display = _text(profile.get("display_name")) or _text(profile.get("real_name"))
        return team, _text(event.get("user")) or "", display, _text(profile.get("name"))
    if kind == "slash_commands":
        team = _text(payload.get("team_id")) or ""
        return team, _text(payload.get("user_id")) or "", None, _text(payload.get("user_name"))
    if kind == "interactive":
        user = _dict(payload.get("user"))
        team = _text(_dict(payload.get("team")).get("id")) or _text(user.get("team_id")) or ""
        handle = _text(user.get("username")) or _text(user.get("name"))
        return team, _text(user.get("id")) or "", None, handle
    return "", "", None, None


def remember_payload_names(
    sessionmaker: async_sessionmaker[AsyncSession], kind: str, payload: dict[str, Any]
) -> None:
    """Store the name a payload carries for its person, if it carries one."""
    team_id, user_id, display, handle = payload_name(kind, payload)
    if not team_id or not user_id or (display is None and handle is None):
        return
    remember_user_name(
        sessionmaker,
        tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
        platform="slack",
        user_id=user_id,
        display_name=display,
        handle=handle,
    )


def event_channel(payload: dict[str, Any]) -> tuple[str, str]:
    """(team id, channel id) of a message event in a public or private channel, else blanks."""
    event = _dict(payload.get("event"))
    channel = _text(event.get("channel")) or ""
    team = _text(payload.get("team_id")) or _text(event.get("team")) or ""
    if event.get("type") not in {"message", "app_mention"} or channel[:1] not in {"C", "G"}:
        return "", ""
    return team, channel


async def remember_event_channel(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Store the name of the channel an event came from, asking Slack once per channel.

    Slack events carry the channel id only. Best-effort: a failed lookup is retried after
    the TTL, never raised.
    """
    from daimon.adapters.slack.interactions import resolve_web_client

    team_id, channel_id = event_channel(payload)
    if not team_id or not channel_id:
        return
    key, now = (team_id, channel_id), time.monotonic()
    if now - _looked_up.get(key, -_LOOKUP_TTL_S) < _LOOKUP_TTL_S:
        return
    if len(_looked_up) >= _LOOKUP_MEMO_SIZE:
        _looked_up.clear()
    _looked_up[key] = now
    try:
        client = await resolve_web_client(runtime, team_id=team_id)
        if client is None:
            return
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except SlackApiError:
        return
    name = _text(_dict(info.get("channel")).get("name"))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    if name:
        remember_channel_names(
            runtime.sessionmaker,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
            platform="slack",
            names={channel_id: name},
        )
