"""Where a Teams channel lives, and whether the caller may read it.

The teams the bot is in come from `teams_installations`, which the adapter
records; a team's channels from Bot Framework, cached briefly by the client.
A team's id is its General channel's id. Reads go through Graph under the
team's resource-specific consent, so who may read is decided here: the caller
must be on the channel's roster, and a private or shared channel is read only
from a turn inside it, since how Bot Framework reports their rosters is not
confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.stores.domain import TeamsInstallationRow
from daimon.core.stores.teams_installations import list_teams_installations
from daimon.core.teams_graph import GraphClient
from fastmcp.exceptions import ToolError

GENERAL = "General"
_THREAD = ";messageid="
_NOT_FOUND = (
    "daimon is not in a team with that channel, or the id is wrong. Use list_channels for "
    "the channels you can read; a thread id is <channel>;messageid=<root>"
)


@dataclass(frozen=True)
class TeamsChannelRef:
    team_id: str
    group_id: str
    team_name: str | None
    channel_id: str
    channel_name: str
    channel_type: str

    @property
    def is_standard(self) -> bool:
        return self.channel_type == "standard"


def split_thread(conversation_id: str) -> tuple[str, str | None]:
    """(channel, root message id) of `19:…;messageid=<root>`; a bare channel has no root."""
    channel, sep, root = conversation_id.partition(_THREAD)
    return channel, (root or None) if sep else None


def thread_id(channel_id: str, root_id: str) -> str:
    return f"{channel_id}{_THREAD}{root_id}"


def require_client(runtime: McpRuntime, auth: AuthIdentity) -> tuple[TeamsBotClient, str]:
    """The Teams client and the caller's Entra id."""
    if runtime.teams_client is None:
        raise ToolError("Teams tools are not configured on this server")
    if auth.platform_user_id is None:
        raise ToolError("teams tools require a teams-bound identity")
    return runtime.teams_client, auth.platform_user_id


def graph_for(client: TeamsBotClient) -> GraphClient:
    return GraphClient(client.http, client.graph_token)


async def installed_teams(runtime: McpRuntime, auth: AuthIdentity) -> list[TeamsInstallationRow]:
    async with runtime.session_factory() as session:
        return await list_teams_installations(session, tenant_id=auth.tenant_id)


async def channels_of(
    client: TeamsBotClient, team: TeamsInstallationRow, *, fresh: bool = False
) -> list[TeamsChannelRef]:
    """The team's channels; none when the bot can no longer list them."""
    try:
        channels = await client.team_channels(team.team_id, fresh=fresh)
    except (httpx.HTTPError, ValueError):
        return []
    return [
        TeamsChannelRef(
            team_id=team.team_id,
            group_id=team.group_id,
            team_name=team.name,
            channel_id=channel.id,
            channel_name=channel.name or GENERAL,
            channel_type=channel.type or "standard",
        )
        for channel in channels
    ]


async def locate_channel(
    runtime: McpRuntime, auth: AuthIdentity, client: TeamsBotClient, channel_id: str
) -> TeamsChannelRef:
    """The channel's team and facts; a stale cache is refreshed once before giving up."""
    teams = await installed_teams(runtime, auth)
    for fresh in (False, True):
        for team in teams:
            for ref in await channels_of(client, team, fresh=fresh):
                if ref.channel_id == channel_id:
                    return ref
    raise ToolError(_NOT_FOUND)


async def require_readable(
    client: TeamsBotClient,
    ref: TeamsChannelRef,
    *,
    caller: str,
    read_policy: ChannelReadPolicy,
    target: str | None = None,
) -> None:
    """Raise unless the caller may read `target` (a thread) or the channel itself."""
    try:
        is_member = await client.is_member(ref.channel_id, caller)
    except (httpx.HTTPError, ValueError) as err:
        raise ToolError("could not confirm you are in that channel, so nothing was read") from err
    if not is_member:
        raise ToolError("you are not a member of that channel, so nothing was read")
    if not ref.is_standard and ref.channel_id not in read_policy.origin_channel_ids:
        raise ToolError(
            f"{ref.channel_name} is a {ref.channel_type} channel: daimon reads it only from a "
            "conversation inside it. Pass this turn's origin_context_id when you are in it."
        )
    read_policy.require(target or ref.channel_id, ref.channel_id if target else None)
