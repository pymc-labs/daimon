"""Discord's `MirrorPoster`: post a mirrored question and answer.

Run by the bot's mirror poller (`daimon.core.mirror_delivery`). The
destination must be a text channel (or, for a thread mirror, a thread) of the
tenant's own guild, and its placement must be known: a thread's parent is
fetched when it is not cached, because its category and channel rules are
what the refusal is checked against. The agent's names are read at delivery,
so a renamed agent meets its current rules. A missing destination or agent, a
rule refusal or a bot that may not post pauses the mirror.

In a channel, a session's first post opens a thread on a short header and
later posts go into it; a thread deleted, locked or closed to the bot is
replaced by a new one. Posts are
the bot's own with mentions disabled, and they are not recorded as agent
posts, so a reply to one starts no turn; the bot neither follows a mirror's
session threads nor hints in them (`is_mirror_thread`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.discord.routine_delivery import channel_placement
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.message_split import split_fenced
from daimon.core.mirror_delivery import MirrorJob, MirrorOutcome, MirrorPoster
from daimon.core.mirrors import answer_limit, mirror_refusal, render_qa
from daimon.core.permissions import channel_permissions
from daimon.core.stores.domain import MirrorRow
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

__all__ = ["AgentNames", "ma_agent_names", "make_discord_mirror_poster"]

log = structlog.get_logger(__name__)

_DISCORD_MAX_CHARS = 2000
_NO_MENTIONS = discord.AllowedMentions.none()

ChannelFetcher = Callable[[int], Awaitable[object]]
#: The current names of a mirror's agent, first its own; None once it is gone.
AgentNames = Callable[[MirrorRow], Awaitable[tuple[str, ...] | None]]


def ma_agent_names(client: AsyncAnthropic) -> AgentNames:
    """Read a mirror's agent names from Managed Agents."""

    async def _names(mirror: MirrorRow) -> tuple[str, ...] | None:
        agent = await find_agent_by_derived_uuid(
            client, tenant_id=mirror.tenant_id, agent_id=mirror.source_agent_id
        )
        if agent is None:
            return None
        return tuple(dict.fromkeys(n for n in agent_pin_names(agent.name, agent.metadata) if n))

    return _names


def _unavailable() -> MirrorOutcome:
    return MirrorOutcome(
        status="skipped", note="destination_unavailable", pause_reason="destination_unavailable"
    )


async def _session_thread(
    thread_id: str | None, channel: discord.TextChannel, fetch_channel: ChannelFetcher
) -> discord.Thread | None:
    """The session's earlier thread under `channel`, or None when it is gone,
    locked or closed to the bot: the session then opens a new one."""
    if thread_id is None:
        return None
    try:
        thread = await fetch_channel(int(thread_id))
    except (discord.NotFound, discord.Forbidden):
        return None
    if isinstance(thread, discord.Thread) and thread.parent_id == channel.id and not thread.locked:
        return thread
    return None


async def _send(target: discord.TextChannel | discord.Thread, texts: tuple[str, ...]) -> None:
    for text in texts:
        for chunk in split_fenced(text, _DISCORD_MAX_CHARS, word_boundary=True):
            await target.send(content=chunk, allowed_mentions=_NO_MENTIONS, suppress_embeds=True)


def make_discord_mirror_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fetch_channel: ChannelFetcher,
    agent_names: AgentNames,
    max_answer_chars: int,
) -> MirrorPoster:
    """A poster bound to the bot's channel lookup (cache first, then REST)."""

    async def _post(job: MirrorJob) -> MirrorOutcome:
        mirror = job.mirror
        async with sessionmaker() as session:
            tenant = await get_tenant(session, mirror.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return MirrorOutcome(status="skipped", note="tenant_archived")
        try:
            channel = await fetch_channel(int(mirror.dest_id))
        except (discord.NotFound, discord.Forbidden):
            return _unavailable()
        except discord.HTTPException as err:
            # A 5xx or rate limit says nothing about the destination: skip, never pause.
            log.warning("mirror.destination_fetch_failed", status=err.status)
            return MirrorOutcome(status="skipped", note="destination_unreachable")
        placement = await channel_placement(channel, fetch_channel)
        kind = discord.Thread if mirror.dest_kind == "thread" else discord.TextChannel
        if (
            placement is None
            or str(placement[2]) != tenant.external_id
            or not isinstance(channel, kind)
        ):
            return _unavailable()
        parent_channel_id, category_id, _guild_id, _source = placement
        dest = channel_permissions(
            job.policy,
            channel_id=str(channel.id),
            parent_channel_id=parent_channel_id,
            category_id=category_id,
        )
        names = await agent_names(mirror)
        if not names:
            return MirrorOutcome(
                status="skipped", note="agent_missing", pause_reason="agent_missing"
            )
        if (refusal := mirror_refusal(job.policy, agent_names=names, dest=dest)) is not None:
            log.info("mirror.delivery_refused", mirror_id=str(mirror.id), reason=refusal)
            return MirrorOutcome(status="skipped", note=f"refused:{refusal}", pause_reason=refusal)
        # A refusal of this session alone skips it; the mirror goes on.
        refusal = mirror_refusal(job.policy, agent_names=names, dest=dest, source=job.event.source)
        if refusal is not None:
            log.info("mirror.delivery_refused", mirror_id=str(mirror.id), reason=refusal)
            return MirrorOutcome(status="skipped", note=f"refused:{refusal}")
        post = render_qa(
            job.event,
            agent_name=names[0],
            max_chars=answer_limit(mirror, max_answer_chars),
        )
        texts = (post.question, post.answer)
        try:
            target: discord.TextChannel | discord.Thread = channel
            if isinstance(channel, discord.TextChannel):
                thread = await _session_thread(job.thread_id, channel, fetch_channel)
                if thread is not None:
                    try:
                        await _send(thread, texts)
                        return MirrorOutcome(status="delivered", dest_thread_id=str(thread.id))
                    except discord.Forbidden:
                        log.info("mirror.session_thread_closed", mirror_id=str(mirror.id))
                header = await channel.send(
                    content=post.header, allowed_mentions=_NO_MENTIONS, suppress_embeds=True
                )
                target = await header.create_thread(name=post.thread_name)
            await _send(target, texts)
        except discord.Forbidden:
            return MirrorOutcome(status="skipped", note="forbidden", pause_reason="bot_cannot_post")
        thread_id = str(target.id) if mirror.dest_kind == "channel" else None
        return MirrorOutcome(status="delivered", dest_thread_id=thread_id)

    return _post
