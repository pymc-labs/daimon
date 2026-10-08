"""Reads a channel thread's history and the mentioned message from Graph.

The shell around `graph` and `context`: it finds the team's Entra group id
(`graph.TeamGroups`), picks the window (thread, delta since the watermark, or
the channel's posts with their replies), attaches the newest replayed images
and files, and turns any Graph failure into an unavailable block plus one
content-free warning, so a turn never fails for want of history.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Mapping, Sequence

import structlog
from daimon.adapters.teams.attachments import ChannelMedia, SharedFile
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.context import (
    CHANNEL_BACKFILL_LIMIT,
    Attached,
    HistoryBlock,
    channel_block,
    channel_media,
    channel_messages,
    classifier_window,
    delta_block,
    delta_messages,
    history_media,
    thread_block,
    thread_messages,
    unavailable_block,
)
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.teams_graph import (
    GraphClient,
    GraphMessage,
    GraphToken,
    GraphUnavailable,
    TeamGroups,
)
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
        self,
        inbound: TeamsInbound,
        *,
        watermark: str | None,
        skip_ids: frozenset[str],
        images: Mapping[str, int] | None = None,
    ) -> HistoryBlock | None:
        """The history to replay; None for a chat, an unavailable block when Graph fails.

        A mention starting a thread gets the channel's recent posts and their
        replies; a reply the thread, or with a numeric `watermark` only what came
        after it. `images` names the images already inlined (a reseed); without
        it the newest replayed images are picked.
        """
        root = root_id(inbound.conversation_id)
        if inbound.kind != "channel" or root is None:
            return None
        try:
            group = await self._group_id(inbound)
            channel, bot = inbound.channel_id, self._bot_app_id
            if inbound.activity_id == root:
                posts = await self._graph.list_channel_messages(
                    group, channel, top=CHANNEL_BACKFILL_LIMIT, expand_replies=True
                )
                skip = skip_ids | {root}
                messages = channel_messages(posts, skip_ids=skip)
                build = functools.partial(
                    channel_block, posts, skip_ids=skip, bot_app_id=bot, channel_id=channel
                )
            else:
                replies = await self._graph.list_replies(group, channel, root)
                if watermark is not None and watermark.isdigit():
                    after = int(watermark)
                    messages = delta_messages(replies, after=after, skip_ids=skip_ids)
                    build = functools.partial(
                        delta_block, replies, after=after, skip_ids=skip_ids, bot_app_id=bot
                    )
                else:
                    root_message = await self._graph.get_message(group, channel, root)
                    messages = thread_messages(root_message, replies, skip_ids=skip_ids)
                    build = functools.partial(
                        thread_block, root_message, replies, skip_ids=skip_ids, bot_app_id=bot
                    )
        except GraphUnavailable as err:
            log.warning("teams.history.unavailable", status=err.status, reason=err.reason)
            return unavailable_block(err.reason or f"graph {err.status}")
        return await self._attach(inbound, group, messages, images, build)

    async def _attach(
        self,
        inbound: TeamsInbound,
        group: str,
        messages: Sequence[GraphMessage],
        images: Mapping[str, int] | None,
        build: Callable[..., HistoryBlock],
    ) -> HistoryBlock:
        found, files = history_media(messages, group_id=group, channel_id=inbound.channel_id)
        downloads: dict[str, str] = {}
        if files and self._files is not None and await self._files.is_available(inbound):
            resolved = await self._files.resolve(
                ChannelMedia(files=tuple(files)), group_id=group, channel_id=inbound.channel_id
            )
            downloads = {
                f.content_url: f.download_url
                for f in resolved.files
                if f.content_url and f.download_url
            }
        if images is not None:
            return build(attached=Attached(images=images, downloads=downloads))
        order = [m.id for m in messages if m.id in found]
        block = build(attached=Attached({i: len(found[i]) for i in order}, downloads))
        return dataclasses.replace(block, image_urls=tuple(u for i in order for u in found[i]))

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

        Shared files get a download URL when the channel's site is granted.
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
        return await self._files.resolve(media, group_id=group, channel_id=inbound.channel_id)

    async def find_card(
        self, conversation_id: str, marker: str, *, group_ids: Sequence[str]
    ) -> str | None:
        """The bot's newest reply in a channel thread whose card holds `marker`, if any.

        A thread's team is not recorded with it, so each installed team is tried.
        """
        root = root_id(conversation_id)
        if root is None:
            return None
        channel = conversation_id.partition(";")[0]
        for group in group_ids:
            try:
                replies = await self._graph.list_replies(group, channel, root)
            except GraphUnavailable:
                continue
            for message in replies.value:
                app = message.sender.application if message.sender is not None else None
                if (
                    app is not None
                    and app.id == self._bot_app_id
                    and any(
                        marker in (attachment.content or "") for attachment in message.attachments
                    )
                ):
                    return message.id
            return None
        return None
