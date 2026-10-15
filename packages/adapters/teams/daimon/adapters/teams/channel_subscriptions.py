"""Graph change notifications for Teams private and shared channels.

Teams sends the bot only the @mentions in those channels, and the
resource-specific consent that delivers every standard channel post cannot
subscribe there. With `DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED` on,
`ChannelSubscriptions` subscribes to each such channel the bot is seen in
(`notice`), under the tenant-consented `ChannelMessage.Read.All`. One task
(`run`) keeps the subscriptions alive: it sleeps until the next renewal is
due, or until a new subscription or a lifecycle notice wakes it. Nothing
polls the channels.

Graph posts notifications to `NOTIFICATIONS_PATH` and lifecycle notices to
`LIFECYCLE_PATH`, both outside Bot Framework's JWT check: each entry is
verified by its client-state secret, of which only a hash is stored. A new
message costs one GET (`includeResourceData` is off, so no certificate) and
goes to `deliver`, where the adapter routes it as a Bot Framework message.
Only `created` changes are read, so an edit never runs anything again. A
`missed` notice reads the channel's newest page since the last message handled;
so does each boot, for what a predecessor answered 202 but never read. Neither
reads further back than `CATCH_UP_WINDOW`. The adapter's admission row keeps a
message read twice from running twice; one read while draining is held for the
next boot's reclaim.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
import structlog
from daimon.adapters.teams.admissions import REPLAY_MAX_AGE
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.stores.domain import TeamsChannelSubscriptionRow
from daimon.core.stores.teams_channel_subscriptions import (
    delete_teams_channel_subscriptions,
    get_teams_channel_subscription,
    insert_teams_channel_subscription,
    list_teams_channel_subscriptions,
    record_teams_channel_watermark,
    update_teams_channel_subscription,
)
from daimon.core.teams_graph import (
    ChangeNotification,
    ChangeNotificationBatch,
    GraphClient,
    GraphMessage,
    GraphUnavailable,
    TeamGroups,
)
from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from microsoft_teams.api import (
    ConversationUpdateActivity,
    MessageActivityInput,
    SentActivity,
    UninstalledActivity,
)
from microsoft_teams.apps import ActivityContext
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

NOTIFICATIONS_PATH = "/api/graph/notifications"
LIFECYCLE_PATH = "/api/graph/lifecycle"
SUBSCRIBED_CHANNEL_TYPES = frozenset({"private", "shared"})
# Graph's ceiling for channel messages is 4320 minutes; an hour under it absorbs clock skew.
SUBSCRIPTION_LIFETIME = timedelta(minutes=4260)
# Renewed this long before expiry, so failed renewals get many retries.
RENEW_MARGIN = timedelta(hours=12)
# Between two refreshes of one subscription, and before a failed read of the table is retried.
RETRY_DELAY = timedelta(minutes=10)
# A channel Graph refused (no consent, app not in it) is not tried again for this long.
REFUSED_BACKOFF_S = 3600.0
# How far back a catch-up reads: older messages would be answered too late.
CATCH_UP_WINDOW = REPLAY_MAX_AGE
MAX_NOTIFICATION_BYTES = 256 * 1024
# Entries in one POST: each verified one is a task and a Graph read.
MAX_NOTIFICATIONS = 100
_MAX_VALIDATION_TOKEN = 4096
_RESOURCE = re.compile(
    r"^/?teams\('([^']+)'\)/channels\('([^']+)'\)/messages\('([^']+)'\)"
    r"(?:/replies\('([^']+)'\))?$"
)
_CHANNEL_ID = re.compile(r"^19:[^/?#'\s]+$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")

Deliver = Callable[[GraphMessage, TeamsChannelSubscriptionRow], Awaitable[None]]
ChannelTypeLookup = Callable[[str, str], Awaitable[str | None]]
# Bot Framework's 403 code for a conversation the bot is not in. No read tells:
# a channel's member list answers whether or not the bot is in it.
NOT_IN_ROSTER = "BotNotInConversationRoster"


class Spawn(Protocol):
    def __call__(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]: ...


@dataclass(frozen=True)
class SeenChannel:
    """What an activity says about the channel it came from; `channel_type` may be unknown."""

    team_id: str
    team_group_id: str | None
    channel_id: str
    channel_type: str | None
    channel_name: str | None = None
    team_name: str | None = None
    bot_name: str | None = None
    service_url: str | None = None


@dataclass(frozen=True)
class MessageAddress:
    group_id: str
    channel_id: str
    message_id: str
    root_id: str | None


def parse_resource(resource: str | None) -> MessageAddress | None:
    """`teams('g')/channels('c')/messages('m')`, or `…/messages('root')/replies('m')`."""
    match = _RESOURCE.match(resource or "")
    if match is None:
        return None
    group, channel, first, reply = match.groups()
    if reply is None:
        return MessageAddress(group, channel, first, None)
    return MessageAddress(group, channel, reply, first)


def channel_resource(group_id: str, channel_id: str) -> str:
    """The subscription resource for a channel's messages; refuses ids that could move it."""
    try:
        group = str(uuid.UUID(group_id))
    except ValueError:
        raise GraphUnavailable("not a group id") from None
    if not _CHANNEL_ID.match(channel_id):
        raise GraphUnavailable("not a channel id")
    return f"/teams/{group}/channels/{channel_id}/messages"


def client_state_hash(client_state: str) -> bytes:
    return hashlib.sha256(client_state.encode()).digest()


def client_state_matches(stored: bytes, given: str | None) -> bool:
    """Constant-time: a forged notification learns nothing from the timing."""
    return hmac.compare_digest(stored, client_state_hash(given or ""))


def renewal_due_at(
    expires_at: datetime, *, not_before: datetime | None = None, due_by: datetime | None = None
) -> datetime:
    """`RENEW_MARGIN` before expiry, or `due_by` if sooner; never before `not_before`."""
    due = expires_at - RENEW_MARGIN
    if due_by is not None:
        due = min(due, due_by)
    return max(due, not_before) if not_before is not None else due


def seconds_until(due: Iterable[datetime], now: datetime) -> float | None:
    """Sleep before the earliest due time; None (sleep until woken) when nothing is due."""
    earliest = min(due, default=None)
    return None if earliest is None else max(0.0, (earliest - now).total_seconds())


def is_subscribed_type(channel_type: str | None) -> bool:
    return (channel_type or "").strip().lower() in SUBSCRIBED_CHANNEL_TYPES


def bot_not_in_roster(err: httpx.HTTPStatusError) -> bool:
    """A send refused because the bot left the conversation; any other refusal is not that."""
    if err.response.status_code != 403:
        return False
    try:
        body: object = err.response.json()
    except ValueError:
        return False
    match body:
        case {"error": {"code": str(code)}}:
            return code == NOT_IN_ROSTER
        case _:
            return False


def _created(message: GraphMessage) -> datetime | None:
    try:
        return datetime.fromisoformat(message.created_date_time or "")
    except ValueError:
        return None


class ChannelSubscriptions:
    """Subscriptions for `tenant_id`'s private and shared channels, and their notifications.

    `ChannelMessage.Read.All` is tenant-wide: Graph serves a channel whether or
    not the bot is in it, and no read tells whether it is. A channel is dropped
    on the bot's removal event, or on a send there refused with
    `BotNotInConversationRoster` when that event was lost.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        graph: GraphClient,
        groups: TeamGroups,
        channel_type: ChannelTypeLookup,
        deliver: Deliver,
        spawn: Spawn,
        tenant_id: uuid.UUID,
        public_url: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
        new_secret: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._graph = graph
        self._groups = groups
        self._channel_type = channel_type
        self._deliver = deliver
        self._spawn = spawn
        self._tenant_id = tenant_id
        base = public_url.rstrip("/")
        self._notification_url = base + NOTIFICATIONS_PATH
        self._lifecycle_url = base + LIFECYCLE_PATH
        self._now = now
        self._clock = clock
        self._new_secret = new_secret
        # Channels already handled by this process, subscribed or not ours to subscribe.
        self._known: set[str] = set()
        self._refused_until: dict[str, float] = {}
        # Channel id -> the earliest its subscription may be refreshed again.
        self._not_before: dict[str, datetime] = {}
        # Channel id -> when to retry a failed recreation, ahead of its renewal.
        self._recreate_at: dict[str, datetime] = {}
        # Graph writes one at a time: a renewal, a lifecycle notice and a removal never race.
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    def notice(self, channel: SeenChannel) -> None:
        """Subscribe in the background to a channel seen for the first time."""
        if channel.channel_id in self._known:
            return
        if self._refused_until.get(channel.channel_id, 0.0) > self._clock():
            return
        self._known.add(channel.channel_id)
        self._spawn(self.ensure(channel), name="teams.channel-subscription")

    async def ensure(self, channel: SeenChannel) -> None:
        """Subscribe to `channel` if it is private or shared and has no subscription yet.

        Unless that is settled (a row exists, or the channel is not ours to
        subscribe), the channel is un-marked so its next sight tries again.
        """
        settled = False
        try:
            async with self._lock:
                settled = await self._ensure(channel)
        except SQLAlchemyError as err:
            log.warning("teams.channel_subscription.create_failed", error=type(err).__name__)
        finally:
            if not settled:
                self._known.discard(channel.channel_id)
        if settled:
            self._wake.set()

    async def _ensure(self, channel: SeenChannel) -> bool:
        kind = channel.channel_type or await self._lookup_type(channel)
        if kind is None:
            return False  # ask again next time
        if not is_subscribed_type(kind):
            return True
        async with self._sessionmaker() as session:
            existing = await get_teams_channel_subscription(
                session, tenant_id=self._tenant_id, channel_id=channel.channel_id
            )
        if existing is not None:
            return True  # `run` keeps it alive
        try:
            group = await self._groups.group_id(channel.team_id, known=channel.team_group_id)
            await self._subscribe(channel, group, kind.strip().lower(), replacing=None)
        except GraphUnavailable as err:
            self._refused_until[channel.channel_id] = self._clock() + REFUSED_BACKOFF_S
            # The permission is tenant-wide, so a 403 means the admin consent is missing.
            log.warning(
                "teams.channel_subscription.create_failed",
                status=err.status,
                reason=err.reason,
                channel_type=kind,
            )
            return False
        return True

    async def _lookup_type(self, channel: SeenChannel) -> str | None:
        try:
            return await self._channel_type(channel.team_id, channel.channel_id)
        except TEAMS_SEND_ERRORS as err:
            log.info("teams.channel_type.lookup_failed", error=type(err).__name__)
            return None

    async def _subscribe(
        self, channel: SeenChannel, group_id: str, channel_type: str, *, replacing: str | None
    ) -> bool:
        """Create a subscription with a fresh secret and record it in place of `replacing`
        (None: no row yet); the watermark stays.

        A subscription that cannot be recorded is deleted again: nothing would renew it.
        False when another process wrote the row first, which keeps its subscription.
        """
        secret = self._new_secret()
        created = await self._graph.create_subscription(
            resource=channel_resource(group_id, channel.channel_id),
            notification_url=self._notification_url,
            lifecycle_url=self._lifecycle_url,
            client_state=secret,
            expires_at=self._now() + SUBSCRIPTION_LIFETIME,
        )
        try:
            async with self._sessionmaker.begin() as session:
                if replacing is None:
                    recorded = await insert_teams_channel_subscription(
                        session,
                        tenant_id=self._tenant_id,
                        channel_id=channel.channel_id,
                        team_id=channel.team_id,
                        group_id=group_id,
                        channel_type=channel_type,
                        channel_name=channel.channel_name,
                        team_name=channel.team_name,
                        bot_name=channel.bot_name,
                        service_url=channel.service_url,
                        subscription_id=created.id,
                        client_state_sha256=client_state_hash(secret),
                        expires_at=created.expiration_date_time,
                    )
                else:
                    recorded = await update_teams_channel_subscription(
                        session,
                        tenant_id=self._tenant_id,
                        channel_id=channel.channel_id,
                        seen=replacing,
                        subscription_id=created.id,
                        client_state_sha256=client_state_hash(secret),
                        expires_at=created.expiration_date_time,
                    )
        except SQLAlchemyError:
            await self._delete_remote(created.id)
            raise
        self._recreate_at.pop(channel.channel_id, None)
        if not recorded:
            await self._delete_remote(created.id)
            log.info("teams.channel_subscription.superseded")
            return False
        log.info("teams.channel_subscription.created", channel_type=channel_type)
        return True

    async def forget(self, *, channel_id: str | None = None, team_id: str | None = None) -> None:
        """The bot left a channel or a team: drop their subscriptions."""
        async with self._lock:
            async with self._sessionmaker.begin() as session:
                removed = await delete_teams_channel_subscriptions(
                    session, tenant_id=self._tenant_id, channel_id=channel_id, team_id=team_id
                )
            for subscription_id in removed:
                await self._delete_remote(subscription_id)
        if channel_id is not None:
            self._known.discard(channel_id)
            self._not_before.pop(channel_id, None)
            self._recreate_at.pop(channel_id, None)
        if team_id is not None:
            self._known.clear()  # cheap to rebuild: a row read per channel seen again
        if removed:
            log.info("teams.channel_subscription.deleted", count=len(removed))

    async def _forget_logged(
        self, *, channel_id: str | None = None, team_id: str | None = None
    ) -> None:
        """`forget` from an activity handler, whose chain must go on whatever the DB says."""
        try:
            await self.forget(channel_id=channel_id, team_id=team_id)
        except SQLAlchemyError as err:
            # The row stays, renewed, until a send there is refused as not in the roster.
            log.warning("teams.channel_subscription.forget_failed", error=type(err).__name__)

    async def _delete_remote(self, subscription_id: str) -> None:
        try:
            await self._graph.delete_subscription(subscription_id)
        except GraphUnavailable as err:
            # Unrenewed, it expires within three days on its own.
            if err.status != 404:
                log.warning("teams.channel_subscription.delete_failed", status=err.status)

    async def on_conversation_update(
        self, ctx: ActivityContext[ConversationUpdateActivity]
    ) -> None:
        """Subscribe when the bot is added to a channel; forget it on removal or deletion."""
        try:
            await self._conversation_update(ctx.activity)
        finally:
            await ctx.next()

    async def _conversation_update(self, activity: ConversationUpdateActivity) -> None:
        data = activity.channel_data
        channel = data.channel if data is not None else None
        team = data.team if data is not None else None
        bot = activity.recipient.id
        if channel is None or not channel.id or team is None or not team.id:
            return
        removed = any(member.id == bot for member in activity.members_removed or [])
        if removed or (data is not None and data.event_type == "channelDeleted"):
            await self._forget_logged(channel_id=channel.id)
        elif any(member.id == bot for member in activity.members_added or []):
            self.notice(
                SeenChannel(
                    team_id=team.id,
                    team_group_id=team.aad_group_id,
                    channel_id=channel.id,
                    channel_type=channel.type,
                    channel_name=channel.name,
                    team_name=team.name,
                    bot_name=activity.recipient.name,
                    service_url=activity.service_url,
                )
            )

    async def on_uninstall(self, ctx: ActivityContext[UninstalledActivity], *, ours: bool) -> None:
        """The app was removed from a team: drop every channel of it.

        An uninstall is team-scope even when its channelData names a channel
        (the one selected in the client), as `TeamInstalls.on_uninstall` reads
        it. One channel's removal arrives as a conversationUpdate.
        """
        try:
            data = ctx.activity.channel_data
            team = data.team if data is not None else None
            if ours and team is not None and team.id:
                await self._forget_logged(team_id=team.id)
        finally:
            await ctx.next()

    def watch(self, sender: TeamsSender) -> TeamsSender:
        """`sender`, reporting a send refused with `BotNotInConversationRoster` to `bot_removed`."""
        return _RemovalWatch(sender, self)

    def bot_removed(self, conversation_id: str) -> None:
        """Bot Framework says the bot is not in that channel: drop it in the background."""
        channel_id = conversation_id.split(";", 1)[0]
        self._spawn(self._forget_logged(channel_id=channel_id), name="teams.channel-removed")

    async def _drop(self, row: TeamsChannelSubscriptionRow, *, reason: str) -> None:
        """Delete the row, unless another process replaced it since, and Graph's
        subscription; the next sight subscribes again."""
        async with self._sessionmaker.begin() as session:
            await delete_teams_channel_subscriptions(
                session,
                tenant_id=self._tenant_id,
                channel_id=row.channel_id,
                seen=row.subscription_id,
            )
        await self._delete_remote(row.subscription_id)
        self._known.discard(row.channel_id)
        self._not_before.pop(row.channel_id, None)
        self._recreate_at.pop(row.channel_id, None)
        log.info("teams.channel_subscription.dropped", reason=reason)

    async def accept(self, notifications: Sequence[ChangeNotification], *, lifecycle: bool) -> bool:
        """Verify each entry's secret and handle it in the background.

        False when an entry claimed a subscription of ours with the wrong
        secret and none verified: the caller answers 401. An unknown
        subscription (one deleted meanwhile) is ignored, not refused, and an
        id that is no GUID is not looked up.
        """
        notifications = [n for n in notifications if _GUID.match(n.subscription_id)]
        ids = sorted({notification.subscription_id for notification in notifications})
        async with self._sessionmaker() as session:
            rows = await list_teams_channel_subscriptions(
                session, tenant_id=self._tenant_id, subscription_ids=ids
            )
        by_id = {row.subscription_id: row for row in rows}
        verified: list[tuple[ChangeNotification, TeamsChannelSubscriptionRow]] = []
        rejected = False
        for notification in notifications:
            row = by_id.get(notification.subscription_id)
            if row is None:
                log.info("teams.channel_notification.ignored", reason="unknown_subscription")
            elif not client_state_matches(row.client_state_sha256, notification.client_state):
                rejected = True
                log.warning("teams.channel_notification.rejected", reason="client_state")
            else:
                verified.append((notification, row))
        for notification, row in verified:
            if lifecycle:
                self._spawn(self.on_lifecycle(notification, row), name="teams.graph-lifecycle")
            else:
                self._spawn(self.on_change(notification, row), name="teams.graph-message")
        return bool(verified) or not rejected

    async def on_change(
        self, notification: ChangeNotification, row: TeamsChannelSubscriptionRow
    ) -> None:
        """Read the new message a notification names and deliver it."""
        if notification.change_type != "created":
            return  # an edit or a deletion never runs a message again
        address = parse_resource(notification.resource)
        if (
            address is None
            or address.group_id.lower() != row.group_id.lower()
            or address.channel_id != row.channel_id
        ):
            log.warning("teams.channel_notification.ignored", reason="resource")
            return
        try:
            message = await self._graph.get_message(
                row.group_id, row.channel_id, address.message_id, root_id=address.root_id
            )
        except GraphUnavailable as err:
            log.warning("teams.channel_message.read_failed", status=err.status, reason=err.reason)
            return
        await self._handle(row, [message])

    async def on_lifecycle(
        self, notification: ChangeNotification, row: TeamsChannelSubscriptionRow
    ) -> None:
        event = notification.lifecycle_event
        log.info("teams.channel_subscription.lifecycle_received", lifecycle_event=event)
        if event == "reauthorizationRequired":
            await self._refresh_current(row, recreate=False)
        elif event == "subscriptionRemoved":
            await self._refresh_current(row, recreate=True)
        elif event == "missed":
            await self.catch_up(row, floor=self._now() - CATCH_UP_WINDOW)
        self._wake.set()

    async def catch_up(self, row: TeamsChannelSubscriptionRow, *, floor: datetime) -> None:
        """Deliver what the channel's newest page holds since the last message handled
        and since `floor`. One read: the page bounds it."""
        if row.last_message_at is None:
            return  # nothing handled yet, so nothing to have missed after
        since = max(row.last_message_at, floor)
        try:
            page = await self._graph.list_channel_messages(
                row.group_id, row.channel_id, expand_replies=True
            )
        except GraphUnavailable as err:
            log.warning("teams.channel_catch_up.read_failed", status=err.status)
            return
        newer: list[tuple[datetime, GraphMessage]] = []
        for post in page.value:
            for message in (post, *post.replies):
                created = _created(message)
                if created is not None and created > since:
                    newer.append((created, message))
        newer.sort(key=lambda pair: pair[0])
        await self._handle(row, [message for _, message in newer])
        log.info("teams.channel_catch_up.read", count=len(newer))

    async def _handle(self, row: TeamsChannelSubscriptionRow, messages: list[GraphMessage]) -> None:
        """Deliver in order, moving the watermark past each."""
        for message in messages:
            await self._deliver(message, row)
            if (created := _created(message)) is None:
                continue
            try:
                async with self._sessionmaker.begin() as session:
                    await record_teams_channel_watermark(
                        session,
                        tenant_id=self._tenant_id,
                        channel_id=row.channel_id,
                        message_id=message.id,
                        at=created,
                    )
            except SQLAlchemyError as err:
                # The message was taken; a stale watermark only widens a later catch-up,
                # which the admission row keeps from running anything twice.
                log.warning("teams.channel_watermark.write_failed", error=type(err).__name__)

    async def run(self) -> None:
        """Catch up once, then renew each subscription before it expires; sleeps in
        between, never polls."""
        await self._catch_up_at_boot()
        while True:
            self._wake.clear()
            try:
                async with self._sessionmaker() as session:
                    rows = await list_teams_channel_subscriptions(
                        session, tenant_id=self._tenant_id
                    )
            except SQLAlchemyError as err:
                log.warning("teams.channel_subscription.list_failed", error=type(err).__name__)
                await asyncio.sleep(RETRY_DELAY.total_seconds())
                continue
            now = self._now()
            due = [row for row in rows if self._due_at(row) <= now]
            for row in due:
                await self._refresh_current(row, recreate=row.channel_id in self._recreate_at)
            if due:
                continue
            delay = seconds_until((self._due_at(row) for row in rows), now)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def _catch_up_at_boot(self) -> None:
        """Read each channel once for what a predecessor answered 202 but never took
        before it exited, within `CATCH_UP_WINDOW`."""
        try:
            async with self._sessionmaker() as session:
                rows = await list_teams_channel_subscriptions(session, tenant_id=self._tenant_id)
        except SQLAlchemyError as err:
            log.warning("teams.channel_subscription.list_failed", error=type(err).__name__)
            return
        floor = self._now() - CATCH_UP_WINDOW
        for row in rows:
            await self.catch_up(row, floor=floor)

    def _due_at(self, row: TeamsChannelSubscriptionRow) -> datetime:
        return renewal_due_at(
            row.expires_at,
            not_before=self._not_before.get(row.channel_id),
            due_by=self._recreate_at.get(row.channel_id),
        )

    async def _refresh_current(self, seen: TeamsChannelSubscriptionRow, *, recreate: bool) -> None:
        """Refresh or recreate `seen`, re-read under the lock.

        `seen` was read before the lock: a `forget` or another notice may have
        dropped or replaced it since. Acting on it then would resurrect a
        dropped channel or orphan a second subscription, so a row that is
        gone, or now holds another subscription, is left alone.
        """
        async with self._lock:
            try:
                async with self._sessionmaker() as session:
                    row = await get_teams_channel_subscription(
                        session, tenant_id=self._tenant_id, channel_id=seen.channel_id
                    )
            except SQLAlchemyError as err:
                self._not_before[seen.channel_id] = self._now() + RETRY_DELAY
                log.warning("teams.channel_subscription.read_failed", error=type(err).__name__)
                return
            if row is None or row.subscription_id != seen.subscription_id:
                log.info("teams.channel_subscription.stale_skipped")
                return
            if recreate:
                await self._replace(row)
            else:
                await self._refresh(row)

    async def _refresh(self, row: TeamsChannelSubscriptionRow) -> None:
        """Renew, or recreate one Graph no longer has. Failures retry after `RETRY_DELAY`."""
        now = self._now()
        # Set first: whatever happens, this subscription is not refreshed again for a while.
        self._not_before[row.channel_id] = now + RETRY_DELAY
        if row.expires_at <= now:
            await self._replace(row)
            return
        try:
            renewed = await self._graph.renew_subscription(
                row.subscription_id, expires_at=now + SUBSCRIPTION_LIFETIME
            )
            async with self._sessionmaker.begin() as session:
                current = await update_teams_channel_subscription(
                    session,
                    tenant_id=self._tenant_id,
                    channel_id=row.channel_id,
                    seen=row.subscription_id,
                    subscription_id=row.subscription_id,
                    expires_at=renewed.expiration_date_time,
                )
        except GraphUnavailable as err:
            if err.status == 404:
                await self._replace(row)
                return
            log.warning("teams.channel_subscription.renew_failed", status=err.status)
            return
        except SQLAlchemyError as err:
            log.warning("teams.channel_subscription.renew_failed", error=type(err).__name__)
            return
        if current:
            log.info("teams.channel_subscription.renewed")
        else:  # another process replaced or dropped it since, and owns the row
            log.info("teams.channel_subscription.stale_skipped")

    async def _drop_logged(self, row: TeamsChannelSubscriptionRow, *, reason: str) -> None:
        try:
            await self._drop(row, reason=reason)
        except SQLAlchemyError as err:
            log.warning("teams.channel_subscription.drop_failed", error=type(err).__name__)

    async def _replace(self, row: TeamsChannelSubscriptionRow) -> None:
        """Replace a removed or expired subscription; drop the row where Graph refuses one.

        Any other failure retries after `RETRY_DELAY`: waiting for the renewal
        would leave the channel without notifications for up to two days.
        """
        self._recreate_at.pop(row.channel_id, None)
        self._not_before[row.channel_id] = self._now() + RETRY_DELAY
        await self._delete_remote(row.subscription_id)
        channel = SeenChannel(
            team_id=row.team_id,
            team_group_id=row.group_id,
            channel_id=row.channel_id,
            channel_type=row.channel_type,
            channel_name=row.channel_name,
            team_name=row.team_name,
            bot_name=row.bot_name,
            service_url=row.service_url,
        )
        try:
            recorded = await self._subscribe(
                channel, row.group_id, row.channel_type, replacing=row.subscription_id
            )
        except GraphUnavailable as err:
            if err.status in (403, 404):
                # 404: the channel or team is gone; 403: the tenant-wide consent was revoked.
                # Either way the next sight of the channel subscribes again.
                log.warning("teams.channel_subscription.recreate_refused", status=err.status)
                await self._drop_logged(row, reason=f"recreate_{err.status}")
                return
            log.warning("teams.channel_subscription.recreate_failed", status=err.status)
        except SQLAlchemyError as err:
            log.warning("teams.channel_subscription.recreate_failed", error=type(err).__name__)
        else:
            if recorded:
                log.info("teams.channel_subscription.recreated")
            return
        self._recreate_at[row.channel_id] = self._now() + RETRY_DELAY


class _RemovalWatch:
    """A sender that reports a channel send refused as not in the roster, then re-raises."""

    def __init__(self, inner: TeamsSender, subscriptions: ChannelSubscriptions) -> None:
        self._inner = inner
        self._subscriptions = subscriptions

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        try:
            return await self._inner.send(conversation_id, activity, service_url=service_url)
        except httpx.HTTPStatusError as err:
            # Only this code: another 403 (an admin block) or a 404 says nothing of membership.
            if conversation_id.startswith("19:") and bot_not_in_roster(err):
                self._subscriptions.bot_removed(conversation_id)
            raise


def notification_route(
    subscriptions: ChannelSubscriptions, *, lifecycle: bool, draining: Callable[[], bool]
) -> Callable[[Request], Awaitable[Response]]:
    """Graph's POST endpoint: the validation handshake, then 202 and work in the background.

    The 503 while draining is not the safety net: uvicorn stops serving before its
    lifespan sets `draining`, so a notice answered 202 is usually still being read
    then. The adapter holds what that delivers (`TeamsApp.handle_graph_message`).
    """

    async def receive(request: Request) -> Response:
        token = request.query_params.get("validationToken")
        if token is not None:
            if len(token) > _MAX_VALIDATION_TOKEN:
                return Response(status_code=400)
            # Graph wants the token back verbatim within 10 s, as text.
            return PlainTextResponse(token, headers={"X-Content-Type-Options": "nosniff"})
        if draining():
            # Graph retries a non-2xx, so a POST seen here reaches the next instance.
            return Response(status_code=503)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_NOTIFICATION_BYTES:
                return Response(status_code=413)
        try:
            batch = ChangeNotificationBatch.model_validate_json(bytes(body))
        except ValidationError:
            return Response(status_code=400)
        if len(batch.value) > MAX_NOTIFICATIONS:
            return Response(status_code=413)
        accepted = await subscriptions.accept(batch.value, lifecycle=lifecycle)
        return Response(status_code=202 if accepted else 401)

    return receive
