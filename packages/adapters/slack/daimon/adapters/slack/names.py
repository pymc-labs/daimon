"""Remember the names Slack hands us on events, slash commands and clicks.

A message event from a person often carries `user_profile` (display and real
name); a slash command and an interaction carry the username. `/billing`
falls back to what is stored here when `users.info` does not answer in time.
Recording is best-effort and in the background (`daimon.core.platform_names`):
it never delays or fails the event.
"""

from __future__ import annotations

from typing import Any, cast

from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import remember_user_name
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
