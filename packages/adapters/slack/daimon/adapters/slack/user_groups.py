"""Slack user groups for DM grants and rules: who is in one, through the runtime's cache.

`usergroups.users.list` needs the `usergroups:read` scope. Any failure (a missing
scope, a deleted group, the network) raises `GroupLookupFailed`, so a grant or
rule naming the group fails closed while ones naming people still work.
"""

from __future__ import annotations

import aiohttp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.native_admins import GroupLookupFailed, GroupMembers
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


async def fetch_user_group_members(client: AsyncWebClient, group_id: str) -> frozenset[str]:
    """The user ids in one Slack user group; raises `GroupLookupFailed`."""
    try:
        response = await client.usergroups_users_list(usergroup=group_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except (TimeoutError, SlackApiError, aiohttp.ClientError) as exc:
        raise GroupLookupFailed(type(exc).__name__) from exc
    users: object = response.get("users")  # pyright: ignore[reportUnknownMemberType]  # SlackResponse.get is untyped
    if not isinstance(users, list):
        raise GroupLookupFailed("no users in the response")
    return frozenset(str(user) for user in users)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON list


def user_group_members(
    runtime: SlackRuntime, client: AsyncWebClient, *, team_id: str, fresh: bool = False
) -> GroupMembers:
    """A user group's members in `team_id`; `fresh` skips the cache (a conversation's start)."""

    async def members(group_id: str) -> frozenset[str]:
        return await runtime.group_members.members(
            ("slack", team_id, group_id),
            lambda: fetch_user_group_members(client, group_id),
            fresh=fresh,
        )

    return members
