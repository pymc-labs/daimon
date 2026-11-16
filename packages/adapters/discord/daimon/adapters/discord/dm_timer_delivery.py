"""Discord delivery for a timer that fires in its owner's DM.

The bot's wake opener hands each `dm:<uuid>` timer row here (via
`daimon.core.continuity.dm_timers`). Before the turn and before each chunk the
tenant's DMs must be on, the owner allowed to receive them and still a human
member of the guild (read over REST). Anything else stops this timer only.
A transient error (5xx, 429, network) before the turn puts the timer back, capped by
the dispatcher; after the turn the delivery is retried in place and then settles, so
a turn is never run twice. Any other Discord error settles. Mentions are off.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp
import structlog
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.dm_timers import DeliveryFailed, DeliveryRetry
from daimon.core.message_split import split_fenced
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.errors import AdmissionDenied
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)

__all__ = ["DELIVERY_BACKOFF", "deliver_dm_timer", "is_transient"]

_DISCORD_MAX_CHARS = 2000
#: Pauses between in-place delivery retries after the turn ran; then it settles.
DELIVERY_BACKOFF = (1.0, 3.0, 8.0)

_T = TypeVar("_T")

RunTimerTurn = Callable[[], Awaitable[str]]
DmOpener = Callable[[int, int], Awaitable[discord.abc.Messageable]]
MemberPresent = Callable[[int, int], Awaitable[bool]]
DmPolicyLookup = Callable[[uuid.UUID], DirectMessagePolicy]


def is_transient(error: BaseException) -> bool:
    """5xx, 429 and network failures; any other answer from Discord is final."""
    if isinstance(error, discord.HTTPException):
        return error.status >= 500 or error.status == 429
    return isinstance(error, aiohttp.ClientError | TimeoutError | OSError)


async def deliver_dm_timer(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    *,
    run_turn: RunTimerTurn,
    open_dm: DmOpener,
    member_present: MemberPresent,
    dm_policy: DmPolicyLookup,
) -> int:
    """Run the DM turn and send its text to the owner in chunks; how many were sent.

    Raises `AdmissionDenied` when the timer may not start or stops partway. A transient
    error before the turn raises `DeliveryRetry`; one that outlasts the in-place retries
    after it raises `DeliveryFailed`. A permanent Discord error propagates and settles.
    """
    owner = row.requester_external_user_id
    sent = 0

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

    async def after_turn(op: Callable[[], Awaitable[_T]]) -> _T:
        """Retry a transient failure in place; the turn already ran and must not repeat."""
        for pause in (*DELIVERY_BACKOFF, None):
            try:
                return await op()
            except (discord.HTTPException, aiohttp.ClientError, OSError) as error:
                if not is_transient(error):
                    raise
                log.warning("dm_timer.transient_error", row_id=str(row.id), sent=sent)
                if pause is None:
                    raise DeliveryFailed(f"delivery failed: {type(error).__name__}") from None
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    try:
        await still_open()
    except (discord.HTTPException, aiohttp.ClientError, OSError) as error:
        if not is_transient(error):
            raise
        log.warning("dm_timer.transient_error", row_id=str(row.id), sent=0)
        raise DeliveryRetry from None
    text = await run_turn()
    guild = await after_turn(still_open)

    async def open_owner_dm() -> discord.abc.Messageable:
        try:
            return await open_dm(guild, int(owner))
        except LookupError:
            # The opener's definite answer: the owner is not a human member.
            log.info("dm_timer.owner_not_a_member", row_id=str(row.id))
            raise AdmissionDenied(
                reason="invoker_not_allowed", detail="owner is not a human member"
            ) from None

    try:
        dm = await after_turn(open_owner_dm)
        for chunk in split_fenced(text, _DISCORD_MAX_CHARS, word_boundary=True):
            if sent:
                await after_turn(still_open)
            await after_turn(
                lambda chunk=chunk: dm.send(
                    content=chunk, allowed_mentions=discord.AllowedMentions.none()
                )
            )
            sent += 1
    except (discord.Forbidden, discord.NotFound):
        log.info("dm_timer.dm_closed", row_id=str(row.id))
        raise AdmissionDenied(reason="invoker_not_allowed") from None
    return sent
