"""The teams the bot is in: recorded for the MCP server, welcomed on install.

Nothing app-only lists the teams an app is in, so every verified channel
activity records its team (once per process, or again when its name changes)
in `teams_installations`, with the Entra group Graph addresses it by. An
install posts a welcome, to the team's General channel or the 1:1 chat (a
group chat gets none); a
removal from a team deletes its row, so channel reads stop finding it, and
removal of the last installation (a team, 1:1 chat or group chat) archives the
tenant. An install, a team seen for the first time, or any activity from the
organisation clears that archive and provisions the tenant again.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

import structlog
from daimon.adapters.teams.card import notice_card
from daimon.adapters.teams.identity import canonical_uuid
from daimon.adapters.teams.lifecycle import SEND_TIMEOUT_S, TEAMS_SEND_ERRORS
from daimon.core.ops_alerts import alert_ops
from daimon.core.stores.teams_installations import (
    CHAT_PREFIX,
    delete_teams_installation,
    record_teams_chat,
    record_teams_installation,
)
from daimon.core.stores.tenants import get_tenant, set_provision_status
from daimon.core.teams_graph import GraphUnavailable, TeamGroups
from microsoft_teams.api import (
    ActivityBase,
    InstalledActivity,
    MessageActivityInput,
    UninstalledActivity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import OpenUrlAction
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


_CHAT_KINDS = ("personal", "groupChat")
# How often an activity re-checks that the tenant is not archived.
_LIVE_CHECK_S = 60.0

_CHAT_LINK = "https://teams.microsoft.com/l/chat/0/0?users={bot_id}"
"""The documented deep link that opens a 1:1 chat with a bot, by its `28:` account id."""


def welcome_lines(bot: str, *, team: bool) -> tuple[str, str, str]:
    """What the welcome says, one idea per line."""
    if team:
        return (
            f"👋 I'm {bot}. I answer in the thread where you mention me.",
            "@mention me in a post or a reply to ask something.",
            'For setup, open a chat with me and say "help me set up".',
        )
    return (
        f"👋 I'm {bot}. I help with questions and data.",
        "Ask a question or send me a data file.",
        'To set things up, say "help me set up". Send `help` for the rest.',
    )


def welcome_text(bot: str, *, team: bool) -> str:
    return "\n\n".join(welcome_lines(bot, team=team))


def chat_link(bot_id: str | None) -> str | None:
    """A link that opens a 1:1 chat with the bot; None unless `bot_id` is a bot account id."""
    return _CHAT_LINK.format(bot_id=bot_id) if bot_id and bot_id.startswith("28:") else None


def welcome_message(bot: str, *, team: bool, bot_id: str | None) -> MessageActivityInput:
    """The welcome as sent: a neutral card, with Open chat for a team."""
    url = chat_link(bot_id) if team else None
    actions = [OpenUrlAction(title="Open chat", url=url)] if url else ()
    return notice_card(welcome_text(bot, team=team), error=False, actions=actions)


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
        reprovision: Callable[[], None] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._groups = groups
        self._tenant_id = tenant_id
        self._entra_tenant_id = canonical_uuid(entra_tenant_id)
        self._alert_url = alert_url
        self._reprovision = reprovision
        # Team id -> the name last recorded (None when the activity named none).
        self._recorded: dict[str, str | None] = {}
        self._chats: set[str] = set()
        self._live_until = 0.0

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    def is_ours(self, activity: ActivityBase) -> bool:
        """The activity comes from the configured organisation."""
        conversation_tenant = canonical_uuid(activity.conversation.tenant_id)
        return self._entra_tenant_id is not None and conversation_tenant == self._entra_tenant_id

    async def observe(self, activity: ActivityBase) -> bool:
        """Record the activity's team or chat; True when a team was new to the table.

        Any activity from the organisation also clears a stale archive, at most once a minute.
        """
        if not self.is_ours(activity):
            return False
        if time.monotonic() >= self._live_until:
            await self.restore()
            self._live_until = time.monotonic() + _LIVE_CHECK_S
        data = activity.channel_data
        team = data.team if data is not None else None
        if team is None or not team.id:
            await self._record_chat(activity)
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
            await self.restore()
        return new

    async def _record_chat(self, activity: ActivityBase) -> None:
        conversation = activity.conversation
        if conversation.conversation_type not in _CHAT_KINDS or conversation.id in self._chats:
            return
        try:
            async with self._sessionmaker.begin() as session:
                await record_teams_chat(
                    session, tenant_id=self._tenant_id, conversation_id=conversation.id
                )
        except SQLAlchemyError as err:
            log.warning("teams.installation.record_failed", error=type(err).__name__)
            return
        self._chats.add(conversation.id)

    async def on_install(self, ctx: ActivityContext[InstalledActivity]) -> None:
        """Record a team install and welcome whoever installed the bot."""
        activity = ctx.activity
        if not self.is_ours(activity):
            return
        await self.restore()
        kind = activity.conversation.conversation_type
        if kind in _CHAT_KINDS:
            await self._record_chat(activity)
        if kind == "groupChat":
            return  # no welcome there yet
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
            await asyncio.wait_for(
                ctx.send(welcome_message(bot, team=is_team, bot_id=activity.recipient.id)),
                SEND_TIMEOUT_S,
            )
        except TEAMS_SEND_ERRORS as err:
            log.warning("teams.welcome.send_failed", error=type(err).__name__)

    async def restore(self) -> None:
        """Clear the tenant's archive and provision it again; a live tenant is left alone."""
        try:
            async with self._sessionmaker() as session:
                tenant = await get_tenant(session, self._tenant_id)
            if tenant is None or tenant.archived_at is None:
                return
            await set_provision_status(
                self._sessionmaker, tenant_id=self._tenant_id, clear_archive=True
            )
        except SQLAlchemyError as err:
            log.warning("teams.tenant.restore_failed", error=type(err).__name__)
            return
        log.info("teams.tenant.restored", tenant_id=str(self._tenant_id))
        if self._reprovision is not None:
            self._reprovision()

    async def on_uninstall(self, ctx: ActivityContext[UninstalledActivity]) -> None:
        """Forget a team or chat the bot was removed from, archiving the tenant with the last."""
        activity = ctx.activity
        team = activity.channel_data.team if activity.channel_data else None
        if not self.is_ours(activity):
            return
        if team is not None and team.id:
            key = team.id
        elif activity.conversation.conversation_type in _CHAT_KINDS:
            key = CHAT_PREFIX + activity.conversation.id
        else:
            return
        try:
            async with self._sessionmaker.begin() as session:
                archived = await delete_teams_installation(
                    session, tenant_id=self._tenant_id, team_id=key
                )
        except SQLAlchemyError as err:
            log.warning("teams.installation.forget_failed", error=type(err).__name__)
            return
        self._recorded.pop(key, None)
        self._chats.discard(activity.conversation.id)
        self._live_until = 0.0
        log.info("teams.installation.removed", team_id=key)
        if archived:
            log.warning("teams.tenant.archived", tenant_id=str(self._tenant_id))
