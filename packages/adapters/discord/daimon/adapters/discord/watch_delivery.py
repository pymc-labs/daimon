"""Discord delivery for watch reports: the owner's DM, or a new public thread per fire.

The bot's report poller (`daimon.core.continuity.watch_reports`) hands each
started `watch_report` row here with its seed. The approved owner is always the
DM recipient: nothing on the row or the card can name anyone else.

A DM report runs as a fresh private DM turn of the owner under watch posture
(`direct_messages.run_dm_watch`). Before the turn and before each chunk the
tenant's DMs must be on, the owner allowed to receive them, still a human member
of the guild (read over REST), and the watch still due to report. Turned-off DMs,
DMs Discord refuses, or a sealed source pause the watch; anything else stops this
report only.

A new-thread report checks the owner's rights on the parent channel over REST
(`require_watch_parent_access`), makes one public thread and records it on the
firing, so a retry reuses it, then runs in that thread on a fresh session. Each
output there is guarded as a thread report is (`require_watch_thread_access`).
Mentions are off on everything posted.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from daimon.adapters.discord.watch_access import require_watch_parent_access
from daimon.core.config import DirectMessagePolicy
from daimon.core.message_split import split_fenced
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import TaskContinuationRow, WatchRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.watches import (
    get_firing,
    get_watch,
    pause_watch,
    record_delivery_thread,
    watch_seed,
)
from daimon.core.turn.errors import AdmissionDenied
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)

__all__ = ["deliver_dm_report", "open_report_thread", "report_thread_name"]

_DISCORD_MAX_CHARS = 2000
_THREAD_NAME_CHARS = 90

#: Runs the DM turn and returns its final text.
RunDmTurn = Callable[[WatchRow], Awaitable[str]]
#: Opens a DM with a human member of a guild: `(guild_id, user_id)`.
DmOpener = Callable[[int, int], Awaitable[discord.abc.Messageable]]
#: Whether `(guild_id, user_id)` is still a human member, read over REST.
MemberPresent = Callable[[int, int], Awaitable[bool]]
DmPolicyLookup = Callable[[uuid.UUID], DirectMessagePolicy]


async def _watch_of(
    sessionmaker: async_sessionmaker[AsyncSession], row: TaskContinuationRow
) -> WatchRow:
    async with sessionmaker() as db:
        firing = await get_firing(db, row.idempotency_key)
        watch = None if firing is None else await get_watch(db, firing.watch_id)
    if watch is None or watch.owner_external_id != row.requester_external_user_id:
        raise AdmissionDenied(reason="invoker_not_allowed")
    return watch


async def _pause(
    sessionmaker: async_sessionmaker[AsyncSession], watch: WatchRow, reason: str
) -> AdmissionDenied:
    async with sessionmaker.begin() as db:
        await pause_watch(db, watch.id, reason=reason)
    return AdmissionDenied(reason="invoker_not_allowed")


async def deliver_dm_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    *,
    run_turn: RunDmTurn,
    open_dm: DmOpener,
    member_present: MemberPresent,
    dm_policy: DmPolicyLookup,
) -> int:
    """Run the DM turn and send its text to the owner in chunks; how many chunks were sent.

    Raises `AdmissionDenied` when the report may not start or stops partway.
    """
    watch = await _watch_of(sessionmaker, row)
    if watch.delivery != "dm":
        raise AdmissionDenied(reason="invoker_not_allowed")
    owner = watch.owner_external_id

    async def guild_id() -> int:
        async with sessionmaker() as db:
            tenant = await get_tenant(db, watch.tenant_id)
            enabled = await dm_enabled(db, tenant_id=watch.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            raise AdmissionDenied(reason="invoker_not_allowed")
        if not enabled:
            raise await _pause(sessionmaker, watch, "Watch paused: Daimon DMs are off here.")
        if not dm_policy(watch.tenant_id).allows(owner):
            raise await _pause(
                sessionmaker, watch, "Watch paused: you can no longer get DMs from Daimon here."
            )
        return int(tenant.external_id)

    async def still_open() -> int:
        guild = await guild_id()
        if not await member_present(guild, int(owner)):
            raise AdmissionDenied(reason="invoker_not_allowed")
        async with sessionmaker() as db:
            if await watch_seed(db, row, now=datetime.now(UTC)) is None:
                raise AdmissionDenied(reason="runs_elsewhere")
        return guild

    await still_open()
    text = await run_turn(watch)
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
        # The owner closed their DMs to Daimon: every later report would fail the same way.
        raise await _pause(
            sessionmaker, watch, "Watch paused: Discord does not let Daimon DM you."
        ) from None
    return sent


def report_thread_name(watch: WatchRow) -> str:
    """`Watch report: <first words of the instructions>`, within Discord's name limit."""
    words = " ".join(watch.instructions.split())
    name = f"Watch report: {words}" if words else "Watch report"
    return name if len(name) <= _THREAD_NAME_CHARS else name[: _THREAD_NAME_CHARS - 1] + "…"


async def open_report_thread(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    *,
    client: discord.Client,
) -> discord.Thread:
    """The public thread this firing reports in: the one it recorded, or a new one.

    The owner's rights on the parent are checked over REST first; a proved loss
    pauses the watch and raises `AdmissionDenied`.
    """
    watch = await _watch_of(sessionmaker, row)
    if watch.delivery != "new_thread":
        raise AdmissionDenied(reason="invoker_not_allowed")
    await require_watch_parent_access(sessionmaker, row, client)
    async with sessionmaker() as db:
        firing = await get_firing(db, row.idempotency_key)
    if firing is None:
        raise AdmissionDenied(reason="runs_elsewhere")
    if firing.delivery_thread_id is not None:
        thread = await client.fetch_channel(int(firing.delivery_thread_id))
        if not isinstance(thread, discord.Thread) or str(thread.parent_id) != row.parent_channel_id:
            raise AdmissionDenied(reason="runs_elsewhere")
        return thread
    parent = client.get_channel(int(row.parent_channel_id)) or await client.fetch_channel(
        int(row.parent_channel_id)
    )
    if not isinstance(parent, discord.TextChannel):
        raise AdmissionDenied(reason="invoker_not_allowed")
    thread = await parent.create_thread(
        name=report_thread_name(watch),
        type=discord.ChannelType.public_thread,
        auto_archive_duration=1440,
    )
    async with sessionmaker.begin() as db:
        recorded = await record_delivery_thread(db, row.idempotency_key, str(thread.id))
    if recorded != str(thread.id):
        log.warning("watch_report.thread_already_recorded", watch_id=str(watch.id))
        raise AdmissionDenied(reason="runs_elsewhere")
    return thread
