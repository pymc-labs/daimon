"""Slack user groups for DM grants and rules: who is in one. The one copy every path uses.

`usergroups.users.list` needs the `usergroups:read` scope. Any failure (a missing
scope, a deleted group, the network) raises `GroupLookupFailed`, so a grant or
rule naming the group fails closed while ones naming people still work.
"""

from __future__ import annotations

from collections.abc import Awaitable

import httpx
from daimon.core.native_admins import GroupLookupFailed, GroupMembers, GroupMembersCache

SLACK_USERGROUP_USERS_URL = "https://slack.com/api/usergroups.users.list"


async def fetch_user_group_members(
    http: httpx.AsyncClient, *, token: str, group_id: str
) -> frozenset[str]:
    """The user ids in one Slack user group; raises `GroupLookupFailed`."""
    try:
        response = await http.get(
            SLACK_USERGROUP_USERS_URL,
            params={"usergroup": group_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data: object = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GroupLookupFailed(type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise GroupLookupFailed("not a JSON object")
    body: dict[str, object] = {str(k): v for k, v in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON object
    users = body.get("users")
    if body.get("ok") is not True or not isinstance(users, list):
        raise GroupLookupFailed(str(body.get("error", "no users in the response")))
    return frozenset(str(user) for user in users)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON list


def user_group_members(
    http: httpx.AsyncClient,
    *,
    token: str,
    team_id: str,
    cache: GroupMembersCache | None = None,
    fresh: bool = False,
) -> GroupMembers:
    """A user group's members in `team_id`, through `cache` when given; `fresh` skips it."""

    async def members(group_id: str) -> frozenset[str]:
        def fetch() -> Awaitable[frozenset[str]]:
            return fetch_user_group_members(http, token=token, group_id=group_id)

        if cache is None:
            return await fetch()
        return await cache.members(("slack", team_id, group_id), fetch, fresh=fresh)

    return members
