"""Discord delivery for a timer that fires in its owner's DM.

The bot's wake opener hands each `dm:<uuid>` timer row here (via
`daimon.core.continuity.dm_timers`). Before the turn and before each chunk the
tenant's DMs must be on, the owner allowed to receive them and still a human
member of the guild (read over REST). Anything else stops this timer only.
Mentions are off on everything posted.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from daimon.core.config import DirectMessagePolicy
from daimon.core.message_split import split_fenced
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.errors import AdmissionDenied
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)

__all__ = ["deliver_dm_timer"]

_DISCORD_MAX_CHARS = 2000

RunTimerTurn = Callable[[], Awaitable[str]]
DmOpener = Callable[[int, int], Awaitable[discord.abc.Messageable]]
MemberPresent = Callable[[int, int], Awaitable[bool]]
DmPolicyLookup = Callable[[uuid.UUID], DirectMessagePolicy]


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

    Raises `AdmissionDenied` when the timer may not start or stops partway.
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
        if not await member_present(guild, int(owner)):
            raise AdmissionDenied(reason="invoker_not_allowed")
        return guild

    await still_open()
    text = await run_turn()
    guild = await still_open()
    sent = 0
    try:
        dm = await open_dm(guild, int(owner))
        for chunk in split_fenced(text, _DISCORD_MAX_CHARS, word_boundary=True):
            if sent:
                await still_open()
            await dm.send(content=chunk, allowed_mentions=discord.AllowedMentions.none())
            sent += 1
    except discord.Forbidden:
        log.info("dm_timer.dm_closed", row_id=str(row.id))
        raise AdmissionDenied(reason="invoker_not_allowed") from None
    return sent
