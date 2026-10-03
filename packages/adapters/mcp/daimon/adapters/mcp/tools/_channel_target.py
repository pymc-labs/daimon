"""The channel a channel-scoped tool acts on, from the id the caller passed.

Budgets, environments, admins and isolation are stored per parent channel, so
a thread id names its channel: Slack's `<channel>:<thread ts>` and Teams'
`<channel>;messageid=<root>` by splitting (a Teams channel id itself holds
":"), a Discord thread through a lookup. `parse_channel_target` only splits;
`resolve_channel` also confirms the caller can see the channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord import resolve_visible_channel
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.adapters.mcp.tools.teams._directory import locate_channel, require_client, split_thread
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError

_EMPTY = "channel_id is empty. Pass the channel's id."


@dataclass(frozen=True)
class ChannelTarget:
    """The parent channel an id names."""

    channel_id: str
    thread_id: str | None = None
    """The Discord thread the id named, whose own seal counts; Slack and Teams
    threads are not channels and carry none."""


def parse_channel_target(platform: str | None, raw: str) -> ChannelTarget:
    """Split a thread id into its channel without any lookup; refuse an empty id.

    A Discord thread id stays as given: only a lookup can name its parent.
    """
    value = raw.strip()
    if platform == "slack":
        value = value.partition(":")[0].strip()
    elif platform == "teams":
        value = split_thread(value)[0].strip()
    if not value:
        raise ToolError(_EMPTY)
    return ChannelTarget(value)


async def resolve_channel(
    runtime: McpRuntime, auth: AuthIdentity, raw: str, *, lenient: bool = False
) -> ChannelTarget:
    """The parent channel `raw` names, once the caller is confirmed to see it.

    `lenient` is for clearing: Slack and Teams ids are only split, and a
    Discord id the lookup refuses is taken as given, so what was stored for a
    channel since deleted or hidden can still be removed.
    """
    parsed = parse_channel_target(auth.platform, raw)
    if lenient and auth.platform != "discord":
        return parsed
    if auth.platform == "discord" and not parsed.channel_id.isdigit():
        raise ToolError(f"{parsed.channel_id!r} is not a Discord channel id")
    try:
        if auth.platform == "discord":
            parent = await resolve_visible_channel(runtime, auth, parsed.channel_id)
            return ChannelTarget(parent, parsed.channel_id if parent != parsed.channel_id else None)
        if auth.platform == "teams":
            return ChannelTarget(await _visible_teams_channel(runtime, auth, parsed.channel_id))
        if auth.platform == "slack":
            return ChannelTarget(await _visible_slack_channel(runtime, auth, parsed.channel_id))
    except ToolError:
        if lenient:
            return parsed
        raise
    raise ToolError("channel tools exist only for Discord, Slack and Teams channels")


async def _visible_slack_channel(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
    client = await slack_web_client(runtime, team_id=_require_team_id(auth))
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
    except SlackApiError as err:
        raise ToolError(f"Slack could not find {channel_id} in this workspace") from err
    channel = cast("dict[str, object]", info["channel"])
    await check_channel_access(client, channel=channel, user_id=_require_slack_identity(auth))
    return channel_id


async def _visible_teams_channel(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
    client, caller = require_client(runtime, auth)
    ref = await locate_channel(runtime, auth, client, channel_id)
    try:
        is_member = await client.is_member(ref.channel_id, caller)
    except (httpx.HTTPError, ValueError) as err:
        raise ToolError("could not confirm you are in that channel") from err
    if not is_member:
        raise ToolError("you are not a member of that channel")
    return ref.channel_id
