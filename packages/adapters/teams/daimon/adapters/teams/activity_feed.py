"""Activity feed notices: a Teams notification that links a person to a message the bot posted.

Sent when a routine posts its result in a channel (to the routine's creator)
and when a card in a channel thread waits on someone's click (to that
person). A 1:1 chat notifies on its own, so only channel messages get one.
Graph's `sendActivityNotification` on the message's team, app-only, with the
reserved `systemDefault` type, which needs no manifest `activities`; the
resource-specific consent `TeamsActivity.Send.Group` covers it
(https://learn.microsoft.com/graph/teams-send-activityfeednotifications).
A notice is a nicety: it runs in the background within `NOTICE_TIMEOUT_S`,
and a failure is logged once and dropped, never retried.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import structlog
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.output_delivery import Spawn
from daimon.core.stores.domain import TeamsInstallationRow
from daimon.core.teams_graph import GRAPH_ROOT, GraphClient, GraphUnavailable, path_segment
from sqlalchemy.exc import SQLAlchemyError

__all__ = ["ActivityFeed", "ChannelHome", "ChannelHomes", "ChannelPost", "chain_id"]

log = structlog.get_logger(__name__)

NOTICE_TIMEOUT_S = 5.0
# Teams shows this much of the preview line.
PREVIEW_MAX_CHARS = 150
_THREAD = ";messageid="
_CHANNEL_SUFFIXES = ("@thread.tacv2", "@thread.skype")

# A channel nobody records is looked up again only after this long: each walk costs
# one Bot Framework call per recorded team, from the budget sends and reactions share.
MISS_TTL_S = 600.0

TeamChannels = Callable[[str], Awaitable[Mapping[str, str]]]
ListTeams = Callable[[], Awaitable[Sequence[TeamsInstallationRow]]]
# The Entra group recorded for a channel (its Files site or its message subscription).
StoredGroup = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class ChannelHome:
    """The team a channel belongs to, as Graph and a deep link name it."""

    group_id: str
    team_name: str | None = None
    channel_name: str | None = None


@dataclass(frozen=True)
class ChannelPost:
    """A message the bot posted in a channel; `root_id` is its thread's root, or itself."""

    home: ChannelHome
    channel_id: str
    message_id: str
    root_id: str


def chain_id(key: uuid.UUID) -> int:
    """A positive Int64 for `key`: a notice with the same chainId replaces the last."""
    return (key.int & 0x7FFF_FFFF_FFFF_FFFF) or 1


def message_link(post: ChannelPost, *, entra_tenant_id: str) -> str:
    """The Teams deep link to `post`, in the documented channel-message form."""
    params = {
        "tenantId": entra_tenant_id,
        "groupId": post.home.group_id,
        "parentMessageId": post.root_id,
        **({"teamName": post.home.team_name} if post.home.team_name else {}),
        **({"channelName": post.home.channel_name} if post.home.channel_name else {}),
        # A channel message id is its creation time in epoch milliseconds.
        **({"createdTime": post.message_id} if post.message_id.isdigit() else {}),
    }
    channel = quote(post.channel_id, safe=":@")
    message = quote(post.message_id, safe="")
    query = urlencode(params, quote_via=quote)
    return f"https://teams.microsoft.com/l/message/{channel}/{message}?{query}"


def notice_body(
    post: ChannelPost,
    *,
    recipient_id: str,
    text: str,
    preview: str,
    entra_tenant_id: str,
    chain: int | None = None,
) -> dict[str, object]:
    """The `sendActivityNotification` request: `text` is the reason line, `preview` below it."""
    body: dict[str, object] = {
        "topic": {
            "source": "text",
            "value": post.home.channel_name or post.home.team_name or "Teams",
            "webUrl": message_link(post, entra_tenant_id=entra_tenant_id),
        },
        "activityType": "systemDefault",
        "previewText": {"content": " ".join(preview.split())[:PREVIEW_MAX_CHARS]},
        "recipient": {
            "@odata.type": "microsoft.graph.aadUserNotificationRecipient",
            "userId": recipient_id,
        },
        "templateParameters": [{"name": "systemDefaultText", "value": text}],
    }
    if chain is not None:
        body["chainId"] = chain
    return body


def split_channel_thread(conversation_id: str) -> tuple[str, str | None] | None:
    """`(channel, thread root)` of a channel conversation id; None for a chat."""
    channel, _, root = conversation_id.partition(_THREAD)
    if not channel.startswith("19:") or not channel.endswith(_CHANNEL_SUFFIXES):
        return None
    return channel, root or None


class ChannelHomes:
    """Which team holds a channel. Cached; a miss for `miss_ttl_s`.

    General's id is its team's. Otherwise the group comes from the caller (an
    inbound activity's team) or a stored row, and only then from walking every
    recorded team's Bot Framework channel list, which may leave out private
    and shared channels.
    """

    def __init__(
        self,
        list_teams: ListTeams,
        team_channels: TeamChannels,
        *,
        stored_group: StoredGroup | None = None,
        clock: Callable[[], float] = time.monotonic,
        miss_ttl_s: float = MISS_TTL_S,
    ) -> None:
        self._list_teams = list_teams
        self._team_channels = team_channels
        self._stored_group = stored_group
        self._clock = clock
        self._miss_ttl_s = miss_ttl_s
        self._homes: dict[str, ChannelHome] = {}
        self._misses: dict[str, float] = {}

    async def locate(self, channel_id: str, *, group_id: str | None = None) -> ChannelHome | None:
        """`channel_id`'s home; `group_id` is its team's Entra group, when the caller knows it."""
        if (home := self._homes.get(channel_id)) is not None:
            return home
        if group_id is None and self._misses.get(channel_id, 0.0) > self._clock():
            return None
        home = await self._find(channel_id, group_id)
        if home is None:
            self._misses[channel_id] = self._clock() + self._miss_ttl_s
            return None
        self._misses.pop(channel_id, None)
        self._homes[channel_id] = home
        return home

    async def _find(self, channel_id: str, group_id: str | None) -> ChannelHome | None:
        teams = await self._list_teams()
        if (general := next((t for t in teams if t.team_id == channel_id), None)) is not None:
            return ChannelHome(general.group_id, general.name, "General")
        if group_id is None and self._stored_group is not None:
            group_id = await self._stored_group(channel_id)
        if group_id is not None:
            team = next((t for t in teams if t.group_id == group_id), None)
            # Names only label the notice: a team never recorded still gets it.
            if team is None:
                return ChannelHome(group_id)
            channels = await self._channels(team) or {}
            return ChannelHome(group_id, team.name, channels.get(channel_id))
        for team in teams:
            if channel_id in (channels := await self._channels(team) or {}):
                return ChannelHome(team.group_id, team.name, channels[channel_id])
        return None

    async def _channels(self, team: TeamsInstallationRow) -> Mapping[str, str] | None:
        try:
            return await self._team_channels(team.team_id)
        except TEAMS_SEND_ERRORS as err:
            log.info("teams.activity_notice.team_skipped", error=type(err).__name__)
            return None


class ActivityFeed:
    """Sends notices in the background; `notify` returns at once and never raises."""

    def __init__(
        self,
        graph: GraphClient,
        homes: ChannelHomes,
        *,
        entra_tenant_id: str,
        spawn: Spawn,
        timeout_s: float = NOTICE_TIMEOUT_S,
    ) -> None:
        self._graph = graph
        self._homes = homes
        self._entra_tenant_id = entra_tenant_id
        self._spawn = spawn
        self._timeout_s = timeout_s

    def notify(
        self,
        conversation_id: str,
        message_id: str,
        *,
        recipient_id: str,
        text: str,
        preview: str,
        chain: int | None = None,
        group_id: str | None = None,
    ) -> None:
        """Point `recipient_id` at `message_id`, posted to `conversation_id`; chats are skipped.

        `group_id` is the team's Entra group when the caller knows it, saving a lookup.
        """
        if (where := split_channel_thread(conversation_id)) is None or not message_id:
            return
        channel_id, root_id = where
        send = self._send(
            channel_id,
            message_id,
            root_id or message_id,
            recipient_id=recipient_id,
            text=text,
            preview=preview,
            chain=chain,
            group_id=group_id,
        )
        self._spawn(send, name="teams.activity-notice")

    async def _send(
        self,
        channel_id: str,
        message_id: str,
        root_id: str,
        *,
        recipient_id: str,
        text: str,
        preview: str,
        chain: int | None,
        group_id: str | None,
    ) -> None:
        try:
            async with asyncio.timeout(self._timeout_s):
                home = await self._homes.locate(channel_id, group_id=group_id)
                if home is None:
                    log.info("teams.activity_notice.failed", reason="team not found")
                    return
                post = ChannelPost(home, channel_id, message_id, root_id)
                body = notice_body(
                    post,
                    recipient_id=recipient_id,
                    text=text,
                    preview=preview,
                    entra_tenant_id=self._entra_tenant_id,
                    chain=chain,
                )
                url = f"{GRAPH_ROOT}/teams/{path_segment(home.group_id)}/sendActivityNotification"
                await self._graph.send("POST", url, body=body, timeout=self._timeout_s)
        except TimeoutError:
            log.info("teams.activity_notice.failed", reason="timed out")
        except GraphUnavailable as err:
            log.info("teams.activity_notice.failed", status=err.status, reason=err.reason)
        except (SQLAlchemyError, *TEAMS_SEND_ERRORS) as err:  # The installs read.
            log.info("teams.activity_notice.failed", reason=type(err).__name__)
