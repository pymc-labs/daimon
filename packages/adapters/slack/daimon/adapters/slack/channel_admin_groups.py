"""Slack user groups as channel admin grants: which named groups a member is in.

Only groups some grant names are looked up (`usergroups.users.list`, which needs
the `usergroups:read` scope), each through the runtime's short cache. A lookup
that fails, for a missing scope, a deleted group or the network, grants nothing.
"""

from __future__ import annotations

import uuid

import aiohttp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    GroupLookupFailed,
    load_member_group_ids,
)
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


async def list_user_groups(client: AsyncWebClient) -> dict[str, str]:
    """The workspace's user groups by id, labelled ``@handle (name)``.

    Raises `GroupLookupFailed`.
    """
    try:
        response = await client.usergroups_list()  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except (TimeoutError, SlackApiError, aiohttp.ClientError) as exc:
        raise GroupLookupFailed(type(exc).__name__) from exc
    raw: object = response.get("usergroups")  # pyright: ignore[reportUnknownMemberType]  # SlackResponse.get is untyped
    if not isinstance(raw, list):
        raise GroupLookupFailed("no usergroups in the response")
    groups: dict[str, str] = {}
    for item in raw:  # pyright: ignore[reportUnknownVariableType]  # JSON list
        if not isinstance(item, dict) or not item.get("id"):  # pyright: ignore[reportUnknownMemberType]
            continue
        handle, name = str(item.get("handle") or ""), str(item.get("name") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        groups[str(item["id"])] = f"@{handle} ({name})" if handle and name else handle or name  # pyright: ignore[reportUnknownArgumentType]
    return groups


async def user_group_ids(
    runtime: SlackRuntime, client: AsyncWebClient, *, tenant_id: uuid.UUID, user_id: str
) -> frozenset[str]:
    """The user groups named by this workspace's grants that `user_id` is in."""

    async def members(group_id: str) -> frozenset[str]:
        return await runtime.group_members.members(
            ("slack", str(tenant_id), group_id),
            lambda: fetch_user_group_members(client, group_id),
        )

    async with runtime.sessionmaker() as session:
        return await load_member_group_ids(
            session,
            tenant_id=tenant_id,
            platform="slack",
            platform_user_id=user_id,
            members=members,
        )


async def channel_admin_caller(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    is_admin: bool,
) -> ChannelAdminCaller:
    """The caller with their grant-named user groups; a workspace admin needs none."""
    groups = (
        frozenset[str]()
        if is_admin
        else await user_group_ids(runtime, client, tenant_id=tenant_id, user_id=user_id)
    )
    return ChannelAdminCaller(platform_user_id=user_id, role_ids=groups, is_server_admin=is_admin)
