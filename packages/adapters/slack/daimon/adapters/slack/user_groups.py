"""Slack user groups for DM grants and rules, through the runtime's client and cache.

The lookup itself is `daimon.core.slack_user_groups`, shared with the DM tools.
"""

from __future__ import annotations

from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.dm_access import DmGroups, groups_by_members
from daimon.core.slack_user_groups import user_group_members
from slack_sdk.web.async_client import AsyncWebClient


def user_group_lookup(
    runtime: SlackRuntime, client: AsyncWebClient, *, team_id: str, fresh: bool = False
) -> DmGroups:
    """A person's user groups in `team_id`; `fresh` skips the cache (a conversation's start)."""
    return groups_by_members(
        user_group_members(
            runtime.http_client,
            token=client.token or "",
            team_id=team_id,
            cache=runtime.group_members,
            fresh=fresh,
        )
    )
