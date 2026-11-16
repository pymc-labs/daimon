"""The platform group lookups a DM grant check from a tool uses (`daimon.core.dm_access`)."""

from __future__ import annotations

import httpx
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._directory import team_rosters
from daimon.core.dm_access import DmGroups, groups_by_members
from daimon.core.slack_user_groups import user_group_members
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

