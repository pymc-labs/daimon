"""Slack search.messages — user-token-only.

Results are already scoped to what the asking user can see (their own xoxp
token). Search may run from any destination; 1:1 DM hits are dropped when
the destination is not a DM with daimon, matching the read-path DM rule.
Group-DM (mpim) hits follow the user's own visibility and are always kept.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    build_connect_hint,
    slack_read_client,
)
from daimon.adapters.mcp.tools.slack._leak_policy import get_destination, is_dm_destination
from daimon.adapters.mcp.tools.slack._models import SlackSearchMatch, SlackSearchResult
from daimon.adapters.mcp.tools.slack._visibility import map_slack_api_error
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError

_SEARCH_LIMIT_CAP = 25


async def _slack_search_messages_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime, auth: AuthIdentity, *, content: str, limit: int
) -> SlackSearchResult:
    user_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    rc = await slack_read_client(runtime, team_id=team_id, slack_user_id=user_id)
    if not rc.runs_as_user:
        hint = build_connect_hint(runtime, team_id=team_id, slack_user_id=user_id, now=time.time())
        raise ToolError("slack search needs the user's connected account" + (hint or ""))
    destination = await get_destination(runtime, auth, now=datetime.now(tz=UTC))
    dm_ok = is_dm_destination(destination)
    try:
        resp = await rc.client.search_messages(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            query=content, count=min(limit, _SEARCH_LIMIT_CAP)
        )
    except SlackApiError as err:
        mapped = map_slack_api_error(err, as_user=True)
        if mapped is not None:
            raise mapped from err
        raise
    messages = cast(dict[str, Any], resp.get("messages") or {})
    raw_matches = cast(list[dict[str, Any]], messages.get("matches") or [])
    total = int(cast(dict[str, Any], messages.get("paging") or {}).get("total") or 0)
    matches: list[SlackSearchMatch] = []
    for m in raw_matches:
        channel = cast(dict[str, Any], m.get("channel") or {})
        if not dm_ok and channel.get("is_im"):
            continue
        matches.append(
            SlackSearchMatch(
                channel_id=str(channel.get("id", "")),
                channel_name=str(channel["name"]) if channel.get("name") else None,
                ts=str(m.get("ts", "")),
                username=str(m["username"]) if m.get("username") else None,
                text=str(m.get("text", "")),
                permalink=str(m["permalink"]) if m.get("permalink") else None,
            )
        )
    return SlackSearchResult(matches=matches, total=total)
