"""Messages posted while the adapter restarted, read back from Graph at boot.

A deploy leaves seconds when the old process no longer serves and the new one
does not yet. Bot Framework does not redeliver an activity posted then, so an
@mention, a thread reply or a group chat message in that gap would be lost.
`RestartCatchUp.run` waits until the HTTP service accepts activities, then
reads each standard channel and group chat that had a message admitted in the
last day (`list_recent_teams_channels`), one page of the newest messages (a
channel's with their replies), and hands those sent within `CATCH_UP_WINDOW`
and before that moment to `deliver`, oldest first. Anything later is Bot
Framework's: its full parse (sender, targeting, quotes) must win the
admission, not Graph's weaker one. The ceiling is the end of lifespan startup,
just before the socket binds; across clock skew and that gap a message can be
seen by both sources (the claim keeps it to one run) or, inside the gap, by
neither. `deliver` admits them like any other message
(`TeamsApp.catch_up_graph_messages`): a message already admitted, by Bot
Framework or by Graph, is not run again.

Read with the app's resource-specific consent (`ChannelMessage.Read.Group`,
`ChatMessage.Read.Chat`), so it needs no tenant-wide consent and runs whether
or not private-channel messages are on. Private and shared channels are left
to their subscriptions (`channel_subscriptions`); a 1:1 chat cannot be read
back through Graph. A conversation Graph refuses is logged and skipped. The
bot's name, which a typed "@name" is matched against, is not in an activity
here: it is taken from a private-channel subscription, else from the bot's own
message or mention in what was read, and without either a typed name is missed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime

import structlog
from daimon.adapters.teams.activity_feed import ListTeams
from daimon.adapters.teams.admissions import ADMISSION_HORIZON, REPLAY_MAX_AGE
from daimon.adapters.teams.identity import GraphSource, display_bot_name
from daimon.core.stores.teams_channel_subscriptions import list_teams_channel_subscriptions
from daimon.core.stores.teams_message_admissions import list_recent_teams_channels
from daimon.core.teams_graph import GraphClient, GraphIdentity, GraphMessage, GraphUnavailable
from daimon.core.teams_threads import is_group_chat
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# How far back a message is still worth answering: a later one would surprise.
CATCH_UP_WINDOW = REPLAY_MAX_AGE
# The most recently active conversations read, one page each.
MAX_CONVERSATIONS = 50
# Graph reads in flight at once.
READ_CONCURRENCY = 4
# How long to wait for the service to start accepting activities before giving up.
SERVING_WAIT_S = 60.0

# A team's standard channels, by id: their names (None when Teams gives none).
StandardChannels = Callable[[str], Awaitable[Mapping[str, str | None]]]
# When the HTTP service began accepting Bot Framework activities.
ServingFrom = Callable[[], Awaitable[datetime]]
# Hand one conversation's messages, oldest first, to the admission path.
Deliver = Callable[[GraphSource, list[GraphMessage]], Awaitable[None]]


def _is_channel(conversation_id: str) -> bool:
    """A channel id: `…@thread.tacv2`, or `…@thread.skype` in a team made before that."""
    return conversation_id.startswith("19:") and conversation_id.endswith(
        ("@thread.tacv2", "@thread.skype")
    )


class RestartCatchUp:
    """Reads back what a restart's gap may have lost, for the tenant `tenant_id`."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        graph: GraphClient,
        list_teams: ListTeams,
        standard_channels: StandardChannels,
        deliver: Deliver,
        serving_from: ServingFrom,
        bot_app_id: str,
        should_stop: Callable[[], bool] = lambda: False,
        service_url: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._tenant_id = tenant_id
        self._graph = graph
        self._list_teams = list_teams
        self._standard_channels = standard_channels
        self._deliver = deliver
        self._serving_from = serving_from
        self._bot_app_id = bot_app_id.lower()
        self._should_stop = should_stop
        self._service_url = service_url
        self._now = now

    async def run(self) -> None:
        """Read each recently active conversation once; log how many and what came back."""
        try:
            async with asyncio.timeout(SERVING_WAIT_S):
                ceiling = await self._serving_from()
        except TimeoutError:
            log.warning("teams.restart_catch_up.not_serving")
            return
        now = self._now()
        try:
            async with self._sessionmaker() as session:
                recent = await list_recent_teams_channels(
                    session,
                    tenant_id=self._tenant_id,
                    since=now - ADMISSION_HORIZON,
                    limit=MAX_CONVERSATIONS,
                )
                subscriptions = await list_teams_channel_subscriptions(
                    session, tenant_id=self._tenant_id
                )
        except SQLAlchemyError as err:
            log.warning("teams.restart_catch_up.list_failed", error=type(err).__name__)
            return
        subscribed = {row.channel_id for row in subscriptions}
        chats = [c for c in recent if is_group_chat(c)]
        # A subscribed channel is read by its own boot catch-up.
        channels = [c for c in recent if _is_channel(c) and c not in subscribed]
        sources = [
            GraphSource(kind="group", channel_id=chat, service_url=self._service_url)
            for chat in chats
        ]
        sources += await self._channel_sources(channels)
        floor = now - CATCH_UP_WINDOW
        gate = asyncio.Semaphore(READ_CONCURRENCY)
        reads = await asyncio.gather(*(self._fetch(s, floor, gate) for s in sources))
        pages = [read for read in reads if read is not None]
        bot_name = next((row.bot_name for row in subscriptions if row.bot_name), None)
        bot_name = display_bot_name(bot_name) or self._name_in(pages)
        counts = await asyncio.gather(
            *(
                self._catch_up(dataclasses.replace(source, bot_name=bot_name), page, floor, ceiling)
                for source, page in pages
            )
        )
        log.info(
            "teams.restart_catch_up.done",
            conversations=len(sources),
            skipped=len(recent) - len(sources),
            messages=sum(counts),
        )

    def _name_in(self, pages: Sequence[tuple[GraphSource, list[GraphMessage]]]) -> str | None:
        """The bot's name as Graph shows it, from its own message or a mention of it."""
        for _, messages in pages:
            for message in messages:
                for identity in self._bot_identities(message):
                    if name := display_bot_name(identity.display_name):
                        return name
        return None

    def _bot_identities(self, message: GraphMessage) -> Iterator[GraphIdentity]:
        """The bot's own identity in `message`'s sender or mentions."""
        senders = [message.sender] + [mention.mentioned for mention in message.mentions]
        for sender in senders:
            app = sender.application if sender is not None else None
            if app is not None and (app.id or "").lower() == self._bot_app_id:
                yield app

    async def _channel_sources(self, channel_ids: Sequence[str]) -> list[GraphSource]:
        """The standard channels among `channel_ids` with the team each belongs to.

        A channel no recorded team lists as standard is left out: a private or
        shared one without a subscription, or one the bot has left.
        """
        if not channel_ids:
            return []
        try:
            teams = await self._list_teams()
        except SQLAlchemyError as err:
            log.warning("teams.restart_catch_up.list_failed", error=type(err).__name__)
            return []
        wanted = set(channel_ids)
        sources: dict[str, GraphSource] = {}
        for team in teams:
            if not wanted - sources.keys():
                break
            # General's id is its team's: no channel list names it.
            names: dict[str, str | None] = {team.team_id: None}
            names.update(await self._standard_channels(team.team_id))
            for channel_id in (wanted & names.keys()) - sources.keys():
                sources[channel_id] = GraphSource(
                    kind="channel",
                    channel_id=channel_id,
                    team_id=team.team_id,
                    group_id=team.group_id,
                    channel_type="standard",
                    channel_name=names[channel_id],
                    team_name=team.name,
                    service_url=self._service_url,
                )
        return [sources[c] for c in channel_ids if c in sources]

    async def _fetch(
        self, source: GraphSource, floor: datetime, gate: asyncio.Semaphore
    ) -> tuple[GraphSource, list[GraphMessage]] | None:
        """One conversation's newest page, flattened; None when Graph refuses or draining."""
        async with gate:
            if self._should_stop():
                return None
            try:
                return source, await self._read(source, floor)
            except GraphUnavailable as err:
                log.warning(
                    "teams.restart_catch_up.read_failed",
                    conversation_type=source.kind,
                    status=err.status,
                    reason=err.reason,
                )
                return None

    async def _catch_up(
        self, source: GraphSource, page: list[GraphMessage], floor: datetime, ceiling: datetime
    ) -> int:
        """Deliver what `page` holds sent after `floor` and before `ceiling`, oldest first.

        An unexpected failure is logged and ends only this conversation.
        """
        newer = [
            (created, message)
            for message in page
            if (created := message.created_at) is not None and floor < created < ceiling
        ]
        newer.sort(key=lambda pair: (pair[0], pair[1].id))
        if not newer or self._should_stop():
            return 0
        try:
            await self._deliver(source, [message for _, message in newer])
        except Exception:
            log.exception("teams.restart_catch_up.deliver_failed", conversation_type=source.kind)
        return len(newer)

    async def _read(self, source: GraphSource, floor: datetime) -> list[GraphMessage]:
        """The newest page's messages, a channel's replies included."""
        if source.kind == "group":
            page = await self._graph.list_chat_messages(source.channel_id)
            posts = page.value
            everything = posts
        else:
            if source.group_id is None:
                raise GraphUnavailable("no team")
            page = await self._graph.list_channel_messages(
                source.group_id, source.channel_id, expand_replies=True
            )
            posts = page.value
            everything = [message for post in posts for message in (post, *post.replies)]
        sent = [created for post in posts if (created := post.created_at) is not None]
        if page.next_link and sent and min(sent) > floor:
            log.warning("teams.restart_catch_up.truncated", posts=len(posts))
        return everything
