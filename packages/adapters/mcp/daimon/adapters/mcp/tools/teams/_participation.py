"""Teams half of the participation tools' scope check: the caller must see what they name.

Mirrors `tools/discord/_participation.py`. A thread is a channel conversation
id with `;messageid=<root>` (the `<thread role="current_thread">` id), a
channel the id before it; the caller's Entra id must be on that channel's
roster, so nobody makes the bot follow (and spend in) a channel they are not in.
"""

from __future__ import annotations

import re

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.thread_participation import ParticipationScope
from fastmcp.exceptions import ToolError

# Channel conversation ids go into a Bot Framework URL path.
_CHANNEL_ID = re.compile(r"19:[\w:@.=+-]+")
_NOT_A_MEMBER = "you are not a member of that Teams channel, or daimon cannot see it"


async def verify_teams_participation_scope(
    runtime: McpRuntime,
    auth: AuthIdentity,
    scope: ParticipationScope,
    scope_id: str | None,
) -> str | None:
    """The thread's channel id for thread scope, None otherwise, once the caller may see it."""
    if scope is ParticipationScope.WORKSPACE or scope_id is None:
        return None
    channel_id, sep, root = scope_id.partition(";messageid=")
    if not _CHANNEL_ID.fullmatch(channel_id):
        raise ToolError("pass a Teams channel id (19:…@thread.tacv2) or a thread id under one")
    if scope is ParticipationScope.THREAD and not (sep and root.isdigit()):
        raise ToolError('thread_id must be the id from <thread role="current_thread">')
    if scope is ParticipationScope.CHANNEL and sep:
        raise ToolError("channel_id names a thread — pass it as thread_id instead")
    client = runtime.teams_client
    if client is None:
        raise ToolError("Teams tools are not configured on this server")
    if auth.platform_user_id is None:
        raise ToolError("teams tools require a teams-bound identity")
    try:
        is_member = await client.is_member(channel_id, auth.platform_user_id)
    except (httpx.HTTPError, ValueError) as err:
        raise ToolError("could not confirm you are in that channel, so nothing changed") from err
    if not is_member:
        raise ToolError(_NOT_A_MEMBER)
    return channel_id if scope is ParticipationScope.THREAD else None
