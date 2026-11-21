"""Discord's `WorkflowPoster`: a workflow's Finish result, ask, reminder, ask update or notice.

Run by the bot's delivery poller next to the routine poster. The channel (or thread,
a channel too) must be in the workflow's own, live server, and the speaker (the run-as
person, or the owner for a notice) must be able to post there themselves; a DM goes to
the person core named. The rules every workflow post passes come from
`daimon.core.workflows.delivery`. A notice is a red notice card; a result is text, with
only the people it tags pinged, then files. An ask carries its buttons on its last message,
opens its thread when asked to, and records where it landed; a reminder replies there and
an update edits the ask, its buttons gone.
"""

from __future__ import annotations

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
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

__all__ = ["make_discord_workflow_poster"]

_DISCORD_MAX_CHARS = 2000
_Placed = discord.TextChannel | discord.Thread | discord.DMChannel


def _skipped(note: str) -> DeliveryOutcome:
    return DeliveryOutcome(status="skipped", note=note)


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
        for index, chunk in enumerate(chunks):
            if index and (refused := await checked(post, rendered.user_ids)) is not None:
                return _skipped(refused)
            last = index == len(chunks) - 1
            if ask is not None and last:
                sent = await channel.send(content=chunk, allowed_mentions=pings, view=ask_view(ask))
                await recorded(row, post, sent, ask)
            else:
                await channel.send(content=chunk, allowed_mentions=pings)
        if not job_payload(row).files:
            return DeliveryOutcome(status="delivered")
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
        return DeliveryOutcome(status="delivered")

    async def recorded(
        row: DeliveryRow, post: WorkflowPost, sent: discord.Message, ask: dict[str, object]
    ) -> None:
        """Where the ask landed; a thread ask opens its thread from the post."""
        thread_id: str | None = None
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
