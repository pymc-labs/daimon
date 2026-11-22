"""Discord's `WorkflowPoster`: a workflow's Finish result, ask, reminder, ask update or notice.

Run by the bot's delivery poller next to the routine poster. The channel (or thread,
a channel too) must be in the workflow's own, live server, and the speaker (the run-as
person, or the owner for a notice) must be able to post there themselves; a DM goes to
the person core named. The rules every workflow post passes come from
`daimon.core.workflows.delivery`. A notice is a red notice card; a result is text, with
only the people it tags pinged, then files. An ask carries its buttons on its last message,
opens its thread when asked to, and records where it landed; a reminder replies there and
an update edits the ask, its buttons gone. A titled post finds its thread by record, then by
exact name among the channel's active and archived threads (unarchived), else opens one; an
origin message not in a thread gets the thread on it.
"""

from __future__ import annotations

from contextlib import suppress
from typing import cast

from anthropic import AsyncAnthropic
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.output_delivery import upload_limit, upload_output_file
from daimon.adapters.discord.routine_delivery import (
    ChannelFetcher,
    DmOpener,
    channel_placement,
    creator_may_post,
)
from daimon.adapters.discord.workflow_asks import ask_view
from daimon.core.config import Settings
from daimon.core.delivery import DeliveryOutcome, WorkflowPoster, files_envelope, job_payload
from daimon.core.membership import MembershipResolver
from daimon.core.message_split import split_fenced
from daimon.core.output_delivery import DeliverableFile
from daimon.core.routine_files import deliver_routine_files
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.web_links import WebAt
from daimon.core.workflows import asks
from daimon.core.workflows.delivery import (
    NOTICE_TITLE,
    WorkflowPost,
    clear_post,
    post_refusal,
    post_text,
    reach_limits,
    titled_thread,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

__all__ = ["make_discord_workflow_poster"]

_DISCORD_MAX_CHARS = 2000
_Placed = discord.TextChannel | discord.Thread | discord.DMChannel


def _skipped(note: str) -> DeliveryOutcome:
    return DeliveryOutcome(status="skipped", note=note)


async def find_thread(channel: discord.TextChannel, title: str) -> str | None:
    """The channel's public thread named exactly `title`, active or archived, else None. A
    private thread is never adopted: the channel's approvers could not see the posts."""
    for thread in await channel.guild.active_threads():
        if thread.parent_id == channel.id and thread.name == title and not thread.is_private():
            return str(thread.id)
    async for thread in channel.archived_threads(limit=None):
        if thread.name == title and not thread.is_private():
            return str(thread.id)
    return None


def _placed(sent: discord.Message, opened: bool) -> dict[str, str | bool | None]:
    """Where a post landed, as the run's step output reads it."""
    at = sent.channel
    thread = isinstance(at, discord.Thread)
    return {
        "channel": str(at.parent_id if thread else at.id),
        "thread": str(at.id) if thread else None,
        "message": str(sent.id),
        "link": sent.jump_url,
        "opened": opened,
    }


def make_discord_workflow_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    links: WebAt,
    fetch_channel: ChannelFetcher,
    open_dm: DmOpener,
    membership_resolver: MembershipResolver | None = None,
    output_client: AsyncAnthropic | None = None,
) -> WorkflowPoster:
    async def placed_refusal(post: WorkflowPost, mentioned: tuple[str, ...]) -> str | None:
        """Where the post goes and why it may not, rechecked before each send."""
        async with sessionmaker() as session:
            tenant = await get_tenant(session, post.row.tenant_id)
        if tenant is None or not post.channel_id.isdigit():
            return "destination_unavailable"
        try:
            channel = await fetch_channel(int(post.channel_id))
        except discord.HTTPException:
            return "destination_unavailable"
        placed = await channel_placement(channel, fetch_channel)
        if placed is None or str(placed[2]) != tenant.external_id:
            return "destination_unavailable"
        parent, category, _guild, permissions = placed
        if not await creator_may_post(channel, permissions, int(post.speaker)):
            return "creator_cannot_post"
        return await post_refusal(
            post,
            place_id=post.channel_id,
            parent_channel_id=parent,
            category_id=category,
            limits=reach_limits(settings),
            mentioned=mentioned,
        )

    async def cleared(row: DeliveryRow) -> WorkflowPost | str:
        return await clear_post(
            sessionmaker,
            row,
            platform="discord",
            dm_policies=settings.direct_message_policies,
            links=links,
            membership_resolver=membership_resolver,
        )

    async def checked(post: WorkflowPost, mentioned: tuple[str, ...]) -> str | None:
        """A DM, reminder or update is placed already; a channel post is checked there."""
        if post.dm_to is not None or post.placed:
            return None
        return await placed_refusal(post, mentioned)

    async def place(post: WorkflowPost) -> discord.abc.Messageable:
        if post.dm_to is not None:
            async with sessionmaker() as session:
                tenant = await get_tenant(session, post.row.tenant_id)
            assert tenant is not None
            return await open_dm(int(tenant.external_id), int(post.dm_to))
        into = post.thread_id if post.placed and post.thread_id else post.channel_id
        channel = await fetch_channel(int(into))
        assert isinstance(channel, discord.abc.Messageable)
        return channel

    async def threaded(post: WorkflowPost, channel: discord.abc.Messageable) -> _Placed:
        """The thread on the origin message, or the one titled `post.title` under the channel."""
        assert isinstance(channel, discord.TextChannel)
        if post.open_on is not None:
            with suppress(discord.NotFound):
                if isinstance(made := await fetch_channel(int(post.open_on)), discord.Thread):
                    return made
            name = post.title or str(post.row.payload.get("workflow") or "Workflow")
            message = channel.get_partial_message(int(post.open_on))
            return await message.create_thread(name=name[:100])

        async def create(title: str) -> str:
            made = await channel.create_thread(name=title, type=discord.ChannelType.public_thread)
            return str(made.id)

        async def find(title: str) -> str | None:
            return await find_thread(channel, title)

        found = await fetch_channel(
            int(
                await titled_thread(
                    sessionmaker, post, platform="discord", create=create, find=find
                )
            )
        )
        assert isinstance(found, discord.Thread)
        if found.archived:
            found = await found.edit(archived=False)
        return found

    async def placed_post(post: WorkflowPost, text: str) -> DeliveryOutcome:
        """A reminder in the ask's thread or DM, or the ask edited to say how it ended."""
        channel = cast(
            "discord.TextChannel | discord.Thread | discord.DMChannel", await place(post)
        )
        ask = channel.get_partial_message(int(post.message_id or 0))
        if post.row.kind == "ask_update":
            await ask.edit(content=text[:_DISCORD_MAX_CHARS], view=None)
        elif post.thread_id:
            await channel.send(
                content=text[:_DISCORD_MAX_CHARS], allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await ask.reply(
                content=text[:_DISCORD_MAX_CHARS], allowed_mentions=discord.AllowedMentions.none()
            )
        return DeliveryOutcome(status="delivered")

    async def _post(row: DeliveryRow) -> DeliveryOutcome:
        post = await cleared(row)
        if isinstance(post, str):
            return _skipped(post)
        rendered = await post_text(sessionmaker, post, "discord")
        if (refused := await checked(post, rendered.user_ids)) is not None:
            return _skipped(refused)
        try:
            if post.placed:
                return await placed_post(post, rendered.text)
            channel = await place(post)
            opened = isinstance(channel, discord.TextChannel) and (
                post.title is not None or post.open_on is not None
            )
            if opened:
                channel = await threaded(post, channel)
        except (discord.HTTPException, LookupError):
            return _skipped("destination_unavailable")
        pings = discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(int(u)) for u in rendered.user_ids]
        )
        if post.notice:
            await channel.send(
                embed=build_notice_embed(rendered.text, title=NOTICE_TITLE),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return DeliveryOutcome(status="delivered")
        ask = asks.ask_of(row.payload) if row.kind == "ask" else None
        chunks = split_fenced(rendered.text, _DISCORD_MAX_CHARS, word_boundary=True)
        first: discord.Message | None = None
        for index, chunk in enumerate(chunks):
            if index and (refused := await checked(post, rendered.user_ids)) is not None:
                return _skipped(refused)
            last = index == len(chunks) - 1
            if ask is not None and last:
                sent = await channel.send(content=chunk, allowed_mentions=pings, view=ask_view(ask))
                await recorded(row, post, sent, ask)
            else:
                sent = await channel.send(content=chunk, allowed_mentions=pings)
            first = first or sent
        done = DeliveryOutcome(
            status="delivered", placed=None if first is None else _placed(first, opened)
        )
        if not job_payload(row).files:
            return done
        if output_client is None:
            raise RuntimeError("Generated file delivery is unavailable; it will not be retried.")

        async def may_post() -> bool:
            again = await cleared(row)
            return not isinstance(again, str) and await checked(again, ()) is None

        async def notice(text: str) -> None:
            await channel.send(content=text, allowed_mentions=discord.AllowedMentions.none())

        async def upload(file: DeliverableFile) -> None:
            await upload_output_file(channel, file)

        await deliver_routine_files(
            output_client,
            files_envelope(row),
            max_bytes=upload_limit(channel),
            post=upload,
            notice=notice,
            may_post=may_post,
        )
        return done

    async def recorded(
        row: DeliveryRow, post: WorkflowPost, sent: discord.Message, ask: dict[str, object]
    ) -> None:
        """Where the ask landed; a thread ask opens its thread from the post."""
        in_thread = post.dm_to is None and isinstance(sent.channel, discord.Thread)
        thread_id = str(sent.channel.id) if in_thread else None
        if row.target.get("open_thread") and isinstance(sent.channel, discord.TextChannel):
            title = str(ask.get("title") or post.row.payload.get("workflow") or "Answer")
            thread_id = str((await sent.create_thread(name=title[:100])).id)
        async with sessionmaker.begin() as session:
            await asks.record_post(
                session,
                row,
                platform="discord",
                channel_id=str(sent.channel.id),
                thread_id=thread_id,
                message_id=str(sent.id),
                link=sent.jump_url,
                at=sent.created_at,
            )

    return _post
