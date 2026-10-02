"""Reads a channel thread's history and the mentioned message from Graph.

The shell around `graph` and `context`: it finds the team's Entra group id
(`graph.TeamGroups`), picks the window
(thread, delta since the watermark, or channel backfill) and turns any Graph
failure into `None` plus one content-free warning, so a turn never fails for
want of history.
"""

from __future__ import annotations

import structlog
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.context import (
    CHANNEL_BACKFILL_LIMIT,
    HistoryBlock,
    channel_block,
    channel_media,
    classifier_window,
    delta_block,
    thread_block,
)
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.teams_graph import GraphClient, GraphToken, GraphUnavailable, TeamGroups
from daimon.core.thread_participation import ClassifierMessage

log = structlog.get_logger(__name__)


def root_id(conversation_id: str) -> str | None:
    """The thread root's message id from `19:…;messageid=<root>`."""
    _, sep, root = conversation_id.partition(";messageid=")
    return root or None if sep else None


class ThreadReader:
    """Graph reads of the thread a message is in, for the bot `bot_app_id`."""

    def __init__(
        self,
        graph: GraphClient,
        teams: TeamGroups,
        *,
        bot_app_id: str,
        files: ChannelFiles | None = None,
    ) -> None:
        self._graph = graph
        self._teams = teams
        self._bot_app_id = bot_app_id
        self._files = files

    @property
    def token(self) -> GraphToken:
        """The Graph token, for downloading a message's hosted images."""
        return self._graph.token

    async def _group_id(self, inbound: TeamsInbound) -> str:
        return await self._teams.group_id(inbound.team_id, known=inbound.team_group_id)

    async def read(
        self, inbound: TeamsInbound, *, watermark: str | None, skip_ids: frozenset[str]
    ) -> HistoryBlock | None:
        """The history to replay, or None for a chat or when Graph cannot be read.

        A mention starting a thread gets the channel's recent posts; a reply the
        thread, or with a numeric `watermark` only what came after it.
        """
        root = root_id(inbound.conversation_id)
        if inbound.kind != "channel" or root is None:
            return None
        try:
            group = await self._group_id(inbound)
            channel, bot = inbound.channel_id, self._bot_app_id
            if inbound.activity_id == root:
                posts = await self._graph.list_channel_messages(
                    group, channel, top=CHANNEL_BACKFILL_LIMIT
                )
                return channel_block(posts, skip_ids=skip_ids | {root}, bot_app_id=bot)
            replies = await self._graph.list_replies(group, channel, root)
            if watermark is not None and watermark.isdigit():
                return delta_block(replies, after=int(watermark), skip_ids=skip_ids, bot_app_id=bot)
            root_message = await self._graph.get_message(group, channel, root)
            return thread_block(root_message, replies, skip_ids=skip_ids, bot_app_id=bot)
        except GraphUnavailable as err:
            log.warning("teams.history.unavailable", status=err.status, reason=err.reason)
            return None

    async def read_window(
        self, inbound: TeamsInbound, *, exclude_ids: frozenset[str], limit: int
    ) -> list[ClassifierMessage]:
        """The thread before a participation burst, for the classifier.

        Raises `GraphUnavailable`: with no thread to judge, the caller stays silent.
        """
        root = root_id(inbound.conversation_id)
        if inbound.kind != "channel" or root is None:
            raise GraphUnavailable("not a channel thread")
        group = await self._group_id(inbound)
        channel = inbound.channel_id
        replies = await self._graph.list_replies(group, channel, root)
        messages = list(replies.value)
        if replies.next_link is None:  # the whole thread fits: the root opens it
            messages.append(await self._graph.get_message(group, channel, root))
        return classifier_window(
            messages, exclude_ids=exclude_ids, limit=limit, bot_app_id=self._bot_app_id
        )

    async def read_media(self, inbound: TeamsInbound) -> ChannelMedia | None:
        """The images and files of every message the turn answers, or None if one is unreadable.

        Shared files get a download URL when the team's site is granted.
        """
        root = root_id(inbound.conversation_id)
        if inbound.kind != "channel" or root is None:
            return None
        images: list[str] = []
        files: list[SharedFile] = []
        try:
            group = await self._group_id(inbound)
            for message_id in inbound.message_ids:
                message = await self._graph.get_message(
                    group, inbound.channel_id, message_id, root_id=root
                )
                media = channel_media(
                    message, group_id=group, channel_id=inbound.channel_id, root_id=root
                )
                images += media.image_urls
                files += media.files
        except GraphUnavailable as err:
            log.warning("teams.media.unavailable", status=err.status, reason=err.reason)
            return None
        media = ChannelMedia(image_urls=tuple(images), files=tuple(files), group_id=group)
        if self._files is None or not media.files:
            return media
        return await self._files.resolve(media, group_id=group)
