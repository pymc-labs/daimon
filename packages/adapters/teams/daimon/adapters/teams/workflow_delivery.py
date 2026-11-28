"""Teams' `WorkflowPoster`: a workflow's Finish result, ask, reminder, ask update or notice.

Run by the app's delivery poller next to the routine poster, for the configured
organisation only. A thread is `<channel>;messageid=<root>`; its channel is what the
rules check. The speaker (the run-as person, or the owner for a notice) must still be
on the channel's roster, the rule the channel tools apply. The rules every workflow
post passes come from `daimon.core.workflows.delivery`. A notice is an error card. A DM
opens the person's 1:1 chat, found through the roster of the target's home channel. An
ask is a card with its buttons and records where it landed (a reply in the thread it
opens answers it); a reminder goes to the same place and an update rewrites the card
without its buttons. Bots upload no files to channel threads, so files are pointed to
on the run's page, or named as not supported without one. A titled post goes in the thread
of the top-level post of that title this workflow made before (by record only), else a new one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.teams.card import (
    TEAMS_LIMIT,
    agent_named,
    notice_adaptive_card,
    notice_card,
)
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.workflow_asks import ask_card
from daimon.core.config import Settings
from daimon.core.delivery import (
    DeliveryOutcome,
    NothingPosted,
    WorkflowPoster,
    files_envelope,
    job_payload,
    sent_first,
)
from daimon.core.message_split import split_fenced
from daimon.core.routine_delivery import resolve_post_identity
from daimon.core.routine_files import unsupported_files_note
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
from microsoft_teams.api import MessageActivityInput
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["make_teams_workflow_poster"]

#: Statuses that mean the conversation refuses the post, not that it failed for now.
_GONE: Final = (400, 403, 404)


def _skipped(note: str) -> DeliveryOutcome:
    return DeliveryOutcome(status="skipped", note=note)


def _thread(channel: str, thread: str | None) -> str:
    if thread is None:
        return channel
    return thread if ";messageid=" in thread else f"{channel};messageid={thread}"


def _files_note(post: WorkflowPost) -> str:
    if not job_payload(post.row).files:
        return ""
    if post.link is not None:
        return "\n\nThe files are on the run's page."
    return unsupported_files_note(files_envelope(post.row))


def make_teams_workflow_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: DirectChats,
    *,
    tenant_id: uuid.UUID,
    settings: Settings,
    links: WebAt,
    client: AsyncAnthropic | None = None,
    default_agent_name: str | None = None,
) -> WorkflowPoster:
    async def speaker(post: WorkflowPost) -> str | None:
        """The Post's `as_agent` name to post under where agent identity is on, else None."""
        if post.as_agent is None or client is None:
            return None
        async with sessionmaker() as session:
            tenant = await get_tenant(session, post.row.tenant_id)
        if tenant is None:
            return None
        identity = await resolve_post_identity(
            sessionmaker,
            client,
            settings,
            tenant_id=post.row.tenant_id,
            agent_name=post.as_agent,
            agent_id=None,
            platform="teams",
            workspace_id=tenant.external_id,
            default_agent_name=default_agent_name,
        )
        return None if identity.builtin else identity.name

    async def place(post: WorkflowPost) -> str | DeliveryOutcome:
        """The conversation to post in, or why the post is skipped."""
        if post.dm_to is not None:
            member = await teams.member(post.channel_id, post.dm_to) if post.channel_id else None
            if member is None:
                return _skipped("recipient_not_on_platform")
            return await teams.open_chat(member)
        if not post.placed and not await teams.member(post.channel_id, post.speaker):
            return _skipped("creator_cannot_post")
        return _thread(post.channel_id, post.thread_id)

    async def post(row: DeliveryRow) -> DeliveryOutcome:
        if row.tenant_id != tenant_id:
            return _skipped("tenant_archived")
        cleared = await clear_post(
            sessionmaker,
            row,
            platform="teams",
            dm_policies=settings.direct_message_policies,
            links=links,
        )
        if isinstance(cleared, str):
            return _skipped(cleared)
        try:
            conversation = await place(cleared)
        except TEAMS_SEND_ERRORS:
            return _skipped("destination_unavailable")
        if isinstance(conversation, DeliveryOutcome):
            return conversation
        rendered = await post_text(sessionmaker, cleared, "teams")
        if not cleared.placed:  # a reminder or update goes where its ask passed the rules
            limits = reach_limits(settings)
            refused = await post_refusal(cleared, place_id=cleared.channel_id, limits=limits)
            if refused is not None:
                return _skipped(refused)
        try:
            if row.kind == "ask_update":
                edit = MessageActivityInput(id=cleared.message_id)
                await teams.post_card(
                    conversation, edit.add_card(notice_adaptive_card(rendered.text, error=False))
                )
                return DeliveryOutcome(status="delivered")
            if cleared.notice:
                card = notice_card(f"{NOTICE_TITLE}\n\n{rendered.text}", error=True)
                await teams.post_card(conversation, card)
                return DeliveryOutcome(status="delivered")
            ask = asks.ask_of(row.payload) if row.kind == "ask" else None
            words = rendered.text + _files_note(cleared)
            chunks = split_fenced(
                words if ask else agent_named(await speaker(cleared), words),
                TEAMS_LIMIT,
                word_boundary=True,
            )
            titled = (
                cleared.title is not None
                and cleared.dm_to is None
                and ";messageid=" not in conversation
            )
            if titled:
                channel = conversation

                async def create(title: str) -> str:
                    return f"{channel};messageid={await teams.post(channel, title)}"

                conversation = await titled_thread(
                    sessionmaker, cleared, platform="teams", create=create
                )
            posted: list[str] = []
            for chunk in chunks[:-1] if ask else chunks:
                posted.append(await sent_first(teams.post(conversation, chunk), first=not posted))
            if ask is None and posted:
                channel, _, thread = conversation.partition(";messageid=")
                return DeliveryOutcome(
                    status="delivered",
                    placed={
                        "channel": channel,
                        "thread": conversation if thread else None,
                        "message": posted[0],
                        "link": None,
                        "opened": titled,
                    },
                )
            if ask is not None:
                card = MessageActivityInput().add_card(ask_card(chunks[-1], ask))
                sent = await sent_first(teams.post_card(conversation, card), first=not posted)
                root = ";messageid=" not in conversation and cleared.dm_to is None
                thread = f"{conversation};messageid={sent}" if root else None
                async with sessionmaker.begin() as session:
                    await asks.record_post(
                        session,
                        row,
                        platform="teams",
                        channel_id=conversation,
                        # A reply in the ask's thread, or the one it opens, answers it.
                        thread_id=conversation if ";messageid=" in conversation else thread,
                        message_id=sent,
                        at=datetime.now(UTC),
                    )
        except (httpx.HTTPStatusError, NothingPosted) as err:
            cause = err.__cause__ if isinstance(err, NothingPosted) else err
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in _GONE:
                return _skipped("destination_unavailable")
            raise
        return DeliveryOutcome(status="delivered")

    return post
