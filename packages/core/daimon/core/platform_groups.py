"""The groups a DM grant or rule can name, with their names read live from the platform.

A Discord role, a Slack user group or a Teams team (its group id). Only ids are stored,
so `merge_groups` keeps a stored id the platform no longer lists, without a name. A
failed read raises `GroupLookupFailed`; Slack names a missing `usergroups:read` scope.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import cast

import httpx
from daimon.core.native_admins import GroupLookupFailed

DISCORD_API = "https://discord.com/api/v10"
SLACK_USERGROUPS_LIST_URL = "https://slack.com/api/usergroups.list"


@dataclass(frozen=True)
class PlatformGroup:
    id: str
    name: str | None


def merge_groups(live: Sequence[PlatformGroup], stored: Iterable[str]) -> tuple[PlatformGroup, ...]:
    """The platform's groups in its order, then stored ids it no longer lists."""
    listed = {g.id for g in live}
    return (*live, *(PlatformGroup(i, None) for i in sorted(set(stored) - listed)))


async def _json(http: httpx.AsyncClient, url: str, **kwargs: object) -> object:
    try:
        response = await http.get(url, timeout=5, **kwargs)  # pyright: ignore[reportArgumentType]  # httpx's own keyword types
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GroupLookupFailed(type(exc).__name__) from exc


async def discord_roles(
    http: httpx.AsyncClient, *, token: str, guild_id: str
) -> tuple[PlatformGroup, ...]:
    """A guild's roles, highest first, without @everyone and bot-managed roles."""
    data = await _json(
        http, f"{DISCORD_API}/guilds/{guild_id}/roles", headers={"Authorization": f"Bot {token}"}
    )
    if not isinstance(data, list):
        raise GroupLookupFailed("not a role list")
    roles = [
        cast("dict[str, object]", r) for r in cast("list[object]", data) if isinstance(r, dict)
    ]
    kept = [
        r
        for r in roles
        if isinstance(r.get("id"), str) and r.get("id") != guild_id and not r.get("managed")
    ]
    kept.sort(key=lambda r: -int(cast("int", r.get("position", 0)) or 0))
    return tuple(PlatformGroup(str(r["id"]), str(r.get("name") or "") or None) for r in kept)


async def slack_user_groups(http: httpx.AsyncClient, *, token: str) -> tuple[PlatformGroup, ...]:
    """A workspace's user groups by name; needs the `usergroups:read` scope."""
    data = await _json(
        http, SLACK_USERGROUPS_LIST_URL, headers={"Authorization": f"Bearer {token}"}
    )
    if not isinstance(data, dict):
        raise GroupLookupFailed("not a JSON object")
    body = cast("dict[str, object]", data)
    groups = body.get("usergroups")
    if body.get("ok") is not True or not isinstance(groups, list):
        raise GroupLookupFailed(str(body.get("error", "no user groups in the response")))
    found = [
        cast("dict[str, object]", g) for g in cast("list[object]", groups) if isinstance(g, dict)
    ]
    named = [
        PlatformGroup(str(g["id"]), str(g.get("name") or g.get("handle") or "") or None)
        for g in found
        if isinstance(g.get("id"), str)
    ]
    return tuple(sorted(named, key=lambda g: (g.name or "").casefold()))
