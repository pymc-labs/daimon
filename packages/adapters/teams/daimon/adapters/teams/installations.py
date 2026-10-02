"""The teams the bot is in: recorded for the MCP server, welcomed on install.

Nothing app-only lists the teams an app is in, so every verified channel
activity records its team (once per process, or again when its name changes)
in `teams_installations`, with the Entra group Graph addresses it by. An
install posts a welcome, to the team's General channel or the 1:1 chat; a
removal from a team deletes its row, so channel reads stop finding it.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from daimon.adapters.teams.identity import GROUP_CHAT_UNSUPPORTED, canonical_uuid
from daimon.adapters.teams.lifecycle import SEND_TIMEOUT_S, TEAMS_SEND_ERRORS
from daimon.core.ops_alerts import alert_ops
from daimon.core.stores.teams_installations import (
    delete_teams_installation,
    record_teams_installation,
)
from daimon.core.teams_graph import GraphUnavailable, TeamGroups
from microsoft_teams.api import ActivityBase, InstalledActivity, UninstalledActivity
from microsoft_teams.apps import ActivityContext
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


def welcome_text(bot: str, *, team: bool) -> str:
    if team:
        return (
            f"👋 Hi, I'm {bot}. @mention me in a post or a reply and I'll answer in that "
            "thread, with the thread (or the channel's recent posts) as context. Commands "
            f"like `setup`, `routines` and `billing` work in a 1:1 chat with me: open one "
            "and send `help`."
        )
    return (
        f"👋 Hi, I'm {bot}. Ask me anything here: every message goes to your agent. "
        "Send `help` for commands, or `setup` to see who answers where."
    )


class TeamInstalls:
    """Records teams for `tenant_id`, resolving each team's Entra group via `groups`."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        groups: TeamGroups,
        *,
        tenant_id: uuid.UUID,
        entra_tenant_id: str,
        alert_url: SecretStr | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._groups = groups
        self._tenant_id = tenant_id
        self._entra_tenant_id = canonical_uuid(entra_tenant_id)
        self._alert_url = alert_url
        # Team id -> the name last recorded (None when the activity named none).
        self._recorded: dict[str, str | None] = {}

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    def is_ours(self, activity: ActivityBase) -> bool:
        """The activity comes from the configured organisation."""
        conversation_tenant = canonical_uuid(activity.conversation.tenant_id)
        return self._entra_tenant_id is not None and conversation_tenant == self._entra_tenant_id

    async def observe(self, activity: ActivityBase) -> bool:
        """Record the activity's team; True when it was new to the table."""
        data = activity.channel_data
        team = data.team if data is not None else None
        if team is None or not team.id or not self.is_ours(activity):
            return False
        if team.id in self._recorded and team.name in (None, self._recorded[team.id]):
            return False
        try:
            group = await self._groups.group_id(team.id, known=canonical_uuid(team.aad_group_id))
            async with self._sessionmaker.begin() as session:
                new = await record_teams_installation(
                    session,
                    tenant_id=self._tenant_id,
                    team_id=team.id,
                    group_id=group,
                    name=team.name,
                )
        except (GraphUnavailable, SQLAlchemyError) as err:
            log.warning("teams.installation.record_failed", error=type(err).__name__)
            return False
        self._recorded[team.id] = team.name or self._recorded.get(team.id)
        if new:
            log.info("teams.installation.recorded", team_id=team.id)
        return new

    async def on_install(self, ctx: ActivityContext[InstalledActivity]) -> None:
        """Record a team install and welcome whoever installed the bot."""
        activity = ctx.activity
        if not self.is_ours(activity):
            return
        kind = activity.conversation.conversation_type
        is_team = kind == "channel"
        if is_team:
            await self.observe(activity)
            team = activity.channel_data.team if activity.channel_data else None
            alert_ops(
                self._alert_url,
                key=f"install:teams:{team.id if team else activity.conversation.id}",
                message=f"New install: Teams team {team.name if team else '(unnamed)'}",
            )
        bot = activity.recipient.name or "daimon"
        try:
            text = (
                GROUP_CHAT_UNSUPPORTED if kind == "groupChat" else welcome_text(bot, team=is_team)
            )
            await asyncio.wait_for(ctx.send(text), SEND_TIMEOUT_S)
        except TEAMS_SEND_ERRORS as err:
            log.warning("teams.welcome.send_failed", error=type(err).__name__)

    async def on_uninstall(self, ctx: ActivityContext[UninstalledActivity]) -> None:
        """Forget a team the bot was removed from; a 1:1 uninstall keeps nothing to clean."""
        activity = ctx.activity
        team = activity.channel_data.team if activity.channel_data else None
        if team is None or not team.id or not self.is_ours(activity):
            return
        try:
            async with self._sessionmaker.begin() as session:
                await delete_teams_installation(session, tenant_id=self._tenant_id, team_id=team.id)
        except SQLAlchemyError as err:
            log.warning("teams.installation.forget_failed", error=type(err).__name__)
            return
        self._recorded.pop(team.id, None)
        log.info("teams.installation.removed", team_id=team.id)
