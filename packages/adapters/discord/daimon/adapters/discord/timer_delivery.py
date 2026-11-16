"""Discord delivery for a timer that fires in its owner's DM or in a plain channel.

The bot's wake opener hands each `dm:<uuid>` or `ch:<uuid>` timer row here (via
`dispatch_scope_timer`). Before the turn and before each chunk the place is checked
afresh: for a DM the tenant's DMs must be on, the owner allowed to receive them and
still a human member of the guild; for a channel the guild, channel and owner are read
over REST into an empty state, never from the gateway cache, and the owner must still
see the channel, the bot may post there and the channel rules allow it. Anything else
settles this timer only, with no message to anyone. A transient error (5xx, 429,
network) before the turn puts the timer back, capped by the dispatcher; after the turn
delivery is retried in place and then settles, so a turn never runs twice. Any other
Discord error settles. Mentions are off.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp
import structlog
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.scope_timers import DeliveryFailed, DeliveryRetry
from daimon.core.message_split import split_fenced
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.protection import protection_state
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord
from discord.state import ConnectionState

log = structlog.get_logger(__name__)

__all__ = [
    "DELIVERY_BACKOFF",
    "ChannelAccess",
    "deliver_channel_timer",
    "deliver_dm_timer",
    "is_transient",
    "require_channel_timer_access",
]

_DISCORD_MAX_CHARS = 2000
#: Pauses between in-place delivery retries after the turn ran; then it settles.
DELIVERY_BACKOFF = (1.0, 3.0, 8.0)


def is_transient(error: BaseException) -> bool:
    """5xx, 429 and network failures; any other answer from Discord is final."""
    if isinstance(error, discord.HTTPException):
        return error.status >= 500 or error.status == 429
    return isinstance(error, aiohttp.ClientError | TimeoutError | OSError)


async def _deliver[A](
    row: TaskContinuationRow,
    *,
    check: Callable[[], Awaitable[A]],
    run_turn: Callable[[A], Awaitable[str]],
    open_place: Callable[[A], Awaitable[discord.abc.Messageable]],
) -> int:
    """Check, run the turn, then send its text in chunks, checking before each one.

    `open_place` raises `LookupError` when the place is definitely gone. Raises
    `AdmissionDenied` when the timer may not start or stops partway. A transient error
    before the turn raises `DeliveryRetry`; one that outlasts the in-place retries after
    it raises `DeliveryFailed`. A permanent Discord error propagates and settles.
    """
    sent = 0

    async def after_turn[T](op: Callable[[], Awaitable[T]]) -> T:
        """Retry a transient failure in place; the turn already ran and must not repeat."""
        for pause in (*DELIVERY_BACKOFF, None):
            try:
                return await op()
            except (discord.HTTPException, aiohttp.ClientError, OSError) as error:
                if not is_transient(error):
                    raise
                log.warning("timer.transient_error", row_id=str(row.id), sent=sent)
                if pause is None:
                    raise DeliveryFailed(f"delivery failed: {type(error).__name__}") from None
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    try:
        found = await check()
    except (discord.HTTPException, aiohttp.ClientError, OSError) as error:
        if not is_transient(error):
            raise
        log.warning("timer.transient_error", row_id=str(row.id), sent=0)
        raise DeliveryRetry from None
    text = await run_turn(found)
    found = await after_turn(check)
    try:
        place = await after_turn(lambda: open_place(found))
        for chunk in split_fenced(text, _DISCORD_MAX_CHARS, word_boundary=True):
            if sent:
                await after_turn(check)
            await after_turn(
                lambda chunk=chunk: place.send(
                    content=chunk, allowed_mentions=discord.AllowedMentions.none()
                )
            )
            sent += 1
    except (discord.Forbidden, discord.NotFound, LookupError):
        log.info("timer.place_closed", row_id=str(row.id))
        raise AdmissionDenied(reason="invoker_not_allowed") from None
    return sent


async def deliver_dm_timer(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    *,
    run_turn: Callable[[], Awaitable[str]],
    open_dm: Callable[[int, int], Awaitable[discord.abc.Messageable]],
    member_present: Callable[[int, int], Awaitable[bool]],
    dm_policy: Callable[[uuid.UUID], DirectMessagePolicy],
) -> int:
    """Run the DM turn and send its text to the owner in chunks; how many were sent.

    `open_dm(guild, user)` raises `LookupError` when the owner is not a human member.
    """
    owner = row.requester_external_user_id

    async def still_open() -> int:
        async with sessionmaker() as db:
            tenant = await get_tenant(db, row.tenant_id)
            enabled = await dm_enabled(db, tenant_id=row.tenant_id)
        if tenant is None or tenant.archived_at is not None or not enabled:
            raise AdmissionDenied(reason="invoker_not_allowed")
        if not dm_policy(row.tenant_id).allows(owner):
            raise AdmissionDenied(reason="invoker_not_allowed")
        guild = int(tenant.external_id)
        try:
            present = await member_present(guild, int(owner))
        except (discord.NotFound, discord.Forbidden):
            present = False
        if not present:
            raise AdmissionDenied(reason="invoker_not_allowed")
        return guild

    async def turn(_guild: int) -> str:
        return await run_turn()

    async def owner_dm(guild: int) -> discord.abc.Messageable:
        return await open_dm(guild, int(owner))

    return await _deliver(row, check=still_open, run_turn=turn, open_place=owner_dm)


@dataclass(frozen=True)
class ChannelAccess:
    """What a fresh REST proof found: the owner and the channel's category."""

    member: discord.Member
    category_id: str | None


def _discard_event(*args: object, **kwargs: object) -> None:
    pass


async def require_channel_timer_access(
    sessions: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    client: discord.Client,
) -> ChannelAccess:
    """Prove over REST that the owner and the bot may still use the timer's channel.

    Raises `AdmissionDenied` on a proved loss. A transient Discord error propagates.
    """
    async with sessions() as db:
        tenant = await get_tenant(db, row.tenant_id)
    if tenant is None or tenant.archived_at is not None or client.user is None:
        raise AdmissionDenied(reason="invoker_not_allowed")
    guild_id = int(tenant.external_id)
    try:
        state = ConnectionState(
            dispatch=_discard_event,
            handlers={},
            hooks={},
            http=client.http,
            intents=discord.Intents.none(),
        )
        guild = discord.Guild(
            data=await client.http.get_guild(guild_id, with_counts=False), state=state
        )
        if guild.id != guild_id or guild.owner_id is None:
            raise ValueError("guild binding changed")
        roles = await guild.fetch_roles()
        guild._roles = {role.id: role for role in roles}  # pyright: ignore[reportPrivateUsage]
        if not any(role.id == guild_id for role in roles):
            raise ValueError("guild roles are incomplete")
        member = await guild.fetch_member(int(row.requester_external_user_id))
        if member.bot or str(member.id) != row.requester_external_user_id:
            raise ValueError("owner is not a human member")
        me = await guild.fetch_member(client.user.id)
        channel = await guild.fetch_channel(int(row.parent_channel_id))
        if not isinstance(channel, discord.TextChannel) or str(channel.id) != row.parent_channel_id:
            raise ValueError("not a text channel")
        category_id = str(channel.category_id) if channel.category_id is not None else None

        async def rest_category() -> tuple[str | None, bool]:
            return category_id, False

        protection = await protection_state(
            sessions,
            tenant_id=row.tenant_id,
            channel_id=row.parent_channel_id,
            thread_id=None,
            resolve_category=rest_category,
        )
        theirs = channel.permissions_for(member)
        ours = channel.permissions_for(me)
        if not (
            protection.may_post and theirs.view_channel and ours.view_channel and ours.send_messages
        ):
            raise ValueError("the channel is closed to the owner or to the bot")
    except (discord.Forbidden, discord.NotFound, ValueError) as error:
        log.info("channel_timer.access_lost", row_id=str(row.id), error=str(error))
        raise AdmissionDenied(reason="invoker_not_allowed") from None
    return ChannelAccess(member=member, category_id=category_id)


async def deliver_channel_timer(
    row: TaskContinuationRow,
    *,
    check_access: Callable[[], Awaitable[ChannelAccess]],
    run_turn: Callable[[ChannelAccess], Awaitable[str]],
    channel_for: Callable[[int], discord.abc.Messageable],
) -> int:
    """Run the channel turn and post its text in the channel in chunks; how many were sent."""

    async def channel(_access: ChannelAccess) -> discord.abc.Messageable:
        return channel_for(int(row.parent_channel_id))

    return await _deliver(row, check=check_access, run_turn=run_turn, open_place=channel)
