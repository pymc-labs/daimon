"""Live lookups of the groups channel admin grants name: Slack groups, Teams teams, Discord roles.

An account's stored groups are as of its last chat turn. Slack lets members
edit user groups by default, and a removed team owner or Discord role holder
would otherwise keep their rights until they next speak, so an MCP call, a
hub read or an OAuth callback re-checks them here (`confirm_stored_group_ids`),
each cached `GROUP_MEMBERS_TTL_S`. A Discord lookup reads the member's roles,
not the role's members. Any failure raises `GroupLookupFailed`, which grants
nothing. The Slack read duplicates the Slack adapter's: adapters must not
import each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp
import httpx
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.channel_admins import GroupLookupFailed, GroupMembers, GroupMembersCache
from daimon.core.github_credentials import decrypt_token
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.teams_graph import GraphClient, GraphUnavailable
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# A verify waits on this lookup; a slow Slack must fail it, not hold the request.
_SLACK_TIMEOUT_S = 10
_DISCORD_API = "https://discord.com/api/v10"


@dataclass(frozen=True)
class DiscordMembers:
    """A guild member's current roles, read with the bot token."""

    token: str
    http: httpx.AsyncClient

    async def roles(self, guild_id: str, user_id: str) -> frozenset[str]:
        """The member's role ids; none once they left the guild."""
        if not (guild_id.isdigit() and user_id.isdigit()):
            raise GroupLookupFailed("not a discord id")
        try:
            response = await self.http.get(
                f"{_DISCORD_API}/guilds/{guild_id}/members/{user_id}",
                headers={"Authorization": f"Bot {self.token}"},
            )
        except httpx.HTTPError as exc:
            raise GroupLookupFailed(type(exc).__name__) from exc
        if response.status_code == 404:
            return frozenset()
        if response.status_code != 200:
            raise GroupLookupFailed(f"discord answered {response.status_code}")
        roles: object = response.json().get("roles")
        if not isinstance(roles, list):
            raise GroupLookupFailed("no roles in the response")
        return frozenset(str(role) for role in roles)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON list


@dataclass(frozen=True)
class GroupLookups:
    """The process's group lookups, shared by the verifier and the tools."""

    sessionmaker: async_sessionmaker[AsyncSession]
    fernet: MultiFernet | None
    teams_client: TeamsBotClient | None
    discord: DiscordMembers | None = None
    cache: GroupMembersCache = field(default_factory=GroupMembersCache)

    def members(self, platform: str, workspace_id: str) -> GroupMembers | None:
        """The lookup for groups of `workspace_id` on `platform`; None when none can run.

        On Discord it takes a user id and gives their roles (`GroupMembers`).
        """
        if platform == "slack" and self.fernet is not None:
            return lambda group_id: self.cache.members(
                ("slack", workspace_id, group_id),
                lambda: self._slack_members(workspace_id, group_id),
            )
        teams = self.teams_client
        if platform == "teams" and teams is not None:
            return lambda group_id: self.cache.members(
                ("teams", group_id), lambda: _team_owner_ids(teams, group_id)
            )
        discord = self.discord
        if platform == "discord" and discord is not None:
            return lambda user_id: self.cache.members(
                ("discord", workspace_id, user_id), lambda: discord.roles(workspace_id, user_id)
            )
        return None

    async def _slack_members(self, team_id: str, group_id: str) -> frozenset[str]:
        assert self.fernet is not None  # narrowed by `members`
        async with self.sessionmaker() as session:
            row = await get_slack_bot_token(session, team_id=team_id)
        if row is None:
            raise GroupLookupFailed("no slack installation")
        try:
            token = decrypt_token(self.fernet, row.encrypted_token)
        except InvalidToken as exc:
            raise GroupLookupFailed("bot token could not be decrypted") from exc
        client = AsyncWebClient(token=token, timeout=_SLACK_TIMEOUT_S)
        try:
            response = await client.usergroups_users_list(usergroup=group_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        except (TimeoutError, SlackApiError, aiohttp.ClientError) as exc:
            raise GroupLookupFailed(type(exc).__name__) from exc
        users: object = response.get("users")  # pyright: ignore[reportUnknownMemberType]  # SlackResponse.get is untyped
        if not isinstance(users, list):
            raise GroupLookupFailed("no users in the response")
        return frozenset(str(user) for user in users)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # JSON list


async def _team_owner_ids(client: TeamsBotClient, group_id: str) -> frozenset[str]:
    try:
        return await GraphClient(client.http, client.graph_token).list_team_owner_ids(group_id)
    except GraphUnavailable as exc:
        raise GroupLookupFailed(exc.reason) from exc


__all__ = ["DiscordMembers", "GroupLookups"]
