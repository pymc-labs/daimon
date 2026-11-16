"""The platform group lookups a DM grant check from a tool uses (`daimon.core.dm_access`)."""

from __future__ import annotations

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.adapters.mcp.tools.teams._directory import team_rosters
from daimon.core.dm_access import DmGroups, groups_by_members
from daimon.core.slack_user_groups import user_group_members
from daimon.core.stores.direct_messages import is_dm_thread
from daimon.core.thread_handoff import switch_dm_groups
from slack_sdk.web.async_client import AsyncWebClient


def slack_http() -> httpx.AsyncClient:
    """A short-lived client for the Slack reads a DM grant needs (user groups)."""
    return httpx.AsyncClient(timeout=10.0)


def slack_groups(http: httpx.AsyncClient, client: AsyncWebClient, *, team_id: str) -> DmGroups:
    """A person's user groups in `team_id`, with the workspace's bot token."""
    return groups_by_members(user_group_members(http, token=client.token or "", team_id=team_id))


def teams_groups(runtime: McpRuntime) -> DmGroups:
    """A person's teams by the installed teams' rosters; unknown without Teams."""
    client = runtime.teams_client
    return groups_by_members(None if client is None else team_rosters(client))


async def handoff_dm_groups(
    runtime: McpRuntime, auth: AuthIdentity, *, platform: str, thread_id: str
) -> frozenset[str] | None:
    """The caller's groups for a hand-over's DM grant check, read before its transaction.

    None outside a DM and on Discord, whose roles come from the membership resolver.
    """
    user_id = auth.platform_user_id
    if platform == "teams":
        groups = teams_groups(runtime)
    elif platform == "slack" and auth.external_id is not None and is_dm_thread(platform, thread_id):
        client = await slack_web_client(runtime, team_id=auth.external_id)
        async with slack_http() as http:
            return await switch_dm_groups(
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                platform=platform,
                thread_id=thread_id,
                user_id=user_id,
                groups=slack_groups(http, client, team_id=auth.external_id),
            )
    else:
        return None
    return await switch_dm_groups(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        platform=platform,
        thread_id=thread_id,
        user_id=user_id,
        groups=groups,
    )
