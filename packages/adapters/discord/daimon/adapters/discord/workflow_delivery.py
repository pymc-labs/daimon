"""Discord's `WorkflowPoster`: post a workflow's Finish result or owner notice.

Run by the bot's delivery poller next to the routine poster. The channel (or thread,
a channel too) must be in the workflow's own, live server, and the speaker (the run-as
person, or the owner for a notice) must be able to post there themselves. The rules
every workflow post passes come from `daimon.core.workflows.delivery`. A notice is a
red notice card; a result is text, with only the people it tags pinged, then files.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.output_delivery import upload_limit, upload_output_file
from daimon.adapters.discord.routine_delivery import (
    ChannelFetcher,
    channel_placement,
    creator_may_post,
)
from daimon.core.config import Settings
from daimon.core.delivery import DeliveryOutcome, WorkflowPoster, files_envelope, job_payload
from daimon.core.membership import MembershipResolver
from daimon.core.message_split import split_fenced
from daimon.core.output_delivery import DeliverableFile
from daimon.core.routine_files import deliver_routine_files
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workflow_deliveries import DeliveryRow
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


def _skipped(note: str) -> DeliveryOutcome:
    return DeliveryOutcome(status="skipped", note=note)


def make_discord_workflow_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    fetch_channel: ChannelFetcher,
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
            web=settings.web,
            membership_resolver=membership_resolver,
        )

    async def _post(row: DeliveryRow) -> DeliveryOutcome:
        post = await cleared(row)
        if isinstance(post, str):
            return _skipped(post)
        rendered = await post_text(sessionmaker, post, "discord")
        if (refused := await placed_refusal(post, rendered.user_ids)) is not None:
            return _skipped(refused)
        channel = await fetch_channel(int(post.channel_id))
        assert isinstance(channel, discord.TextChannel | discord.Thread)
        pings = discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(int(u)) for u in rendered.user_ids]
        )
        if post.notice:
            await channel.send(
                embed=build_notice_embed(rendered.text, title=NOTICE_TITLE),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return DeliveryOutcome(status="delivered")
        for index, chunk in enumerate(
            split_fenced(rendered.text, _DISCORD_MAX_CHARS, word_boundary=True)
        ):
            if index and (refused := await placed_refusal(post, rendered.user_ids)) is not None:
                return _skipped(refused)
            await channel.send(content=chunk, allowed_mentions=pings)
        if not job_payload(row).files:
            return DeliveryOutcome(status="delivered")
        if output_client is None:
            raise RuntimeError("Generated file delivery is unavailable; it will not be retried.")

        async def may_post() -> bool:
            again = await cleared(row)
            return not isinstance(again, str) and await placed_refusal(again, ()) is None

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

    return _post
