"""Slack's `WorkflowPoster`: a workflow's Finish result, ask, reminder, ask update or notice.

Run by the app's delivery poller next to the routine poster, with a client for the
workflow's own, live workspace. The channel must take the post as it would the
speaker's own (`destination_refusal`, the routine rule), and a thread
(`<channel>:<ts>`) must still exist. The rules every workflow post passes come from
`daimon.core.workflows.delivery`. A notice is an error card; a result is text escaped
as agent replies are (people may be tagged, never `<!channel>`), then files. A DM opens
the person's conversation with the app. An ask carries its buttons and records where it
landed (a thread under it answers it); a reminder replies in that thread and an update
rewrites the ask without its buttons.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, cast

from daimon.adapters.slack.blockkit import error_blocks
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.output_delivery import SLACK_MAX_BYTES_PER_FILE, build_output_poster
from daimon.adapters.slack.routine_delivery import destination_refusal
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.workflow_asks import ask_blocks
from daimon.adapters.slack.workflow_cards import web_at
from daimon.core.delivery import DeliveryOutcome, WorkflowPoster, files_envelope, job_payload
from daimon.core.message_split import split_fenced
from daimon.core.routine_delivery import DeliveryTarget
from daimon.core.routine_files import deliver_routine_files
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.workflows import asks
from daimon.core.workflows.delivery import (
    NOTICE_TITLE,
    WorkflowPost,
    clear_post,
    post_refusal,
    post_text,
    reach_limits,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["make_slack_workflow_poster"]


def _skipped(note: str) -> DeliveryOutcome:
    return DeliveryOutcome(status="skipped", note=note)


def make_slack_workflow_poster(runtime: SlackRuntime) -> WorkflowPoster:
    async def refusal(
        client: AsyncWebClient, post: WorkflowPost, mentioned: tuple[str, ...]
    ) -> str | None:
        if post.dm_to is not None or post.placed:
            return None  # a DM, or where the ask already went
        target = DeliveryTarget(channel_id=post.channel_id, thread_ts=post.thread_id)
        if (refused := await destination_refusal(client, post.speaker, target)) is not None:
            return refused
        return await post_refusal(
            post,
            place_id=post.channel_id,
            limits=reach_limits(runtime.settings),
            mentioned=mentioned,
        )

    async def cleared(row: DeliveryRow) -> WorkflowPost | str:
        return await clear_post(runtime.sessionmaker, row, platform="slack", links=web_at(runtime))

    async def _post(row: DeliveryRow) -> DeliveryOutcome:
        post = await cleared(row)
        if isinstance(post, str):
            return _skipped(post)
        async with runtime.sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
        if tenant is None:
            return _skipped("tenant_archived")
        client = await resolve_web_client(runtime, team_id=tenant.external_id)
        if client is None:
            return _skipped("tenant_archived")
        rendered = await post_text(runtime.sessionmaker, post, "slack")
        if (refused := await refusal(client, post, rendered.user_ids)) is not None:
            return _skipped(refused)
        channel = post.channel_id
        if post.dm_to is not None:
            opened = await client.conversations_open(users=post.dm_to)  # pyright: ignore[reportUnknownMemberType]
            channel = cast("dict[str, str]", opened["channel"])["id"]
        thread = None if post.thread_id is None else post.thread_id.rsplit(":", 1)[-1]
        if row.kind == "reminder" and thread is None:
            thread = post.message_id
        place: dict[str, Any] = {"channel": channel}
        if thread is not None:
            place["thread_ts"] = thread
        text = escape_mrkdwn_preserving_mentions(rendered.text)
        if row.kind == "ask_update":
            await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                ts=post.message_id or "",
                text=text,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}],
            )
            return DeliveryOutcome(status="delivered")
        if post.notice:
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                text=text, blocks=error_blocks(NOTICE_TITLE, rendered.text), **place
            )
            return DeliveryOutcome(status="delivered")
        ask = asks.ask_of(row.payload) if row.kind == "ask" else None
        chunks = split_fenced(text, 3000 if ask else 11800, word_boundary=True)
        for index, chunk in enumerate(chunks):
            if ask is None or index < len(chunks) - 1:
                await client.chat_postMessage(text=chunk, **place)  # pyright: ignore[reportUnknownMemberType]
                continue
            sent = await client.chat_postMessage(text=chunk, blocks=ask_blocks(chunk, ask), **place)  # pyright: ignore[reportUnknownMemberType]
            ts = str(sent.get("ts"))  # pyright: ignore[reportUnknownMemberType]
            link: str | None = None
            with suppress(SlackApiError):
                # A missing link only hides "open in Slack" on the run page; the ask stands.
                found = await client.chat_getPermalink(channel=channel, message_ts=ts)  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                link = str(found.get("permalink") or "") or None  # pyright: ignore[reportUnknownMemberType]
            async with runtime.sessionmaker.begin() as session:
                await asks.record_post(
                    session,
                    row,
                    platform="slack",
                    channel_id=channel,
                    # A thread under the ask answers it, unless it went into a thread already.
                    thread_id=None if thread is not None else f"{channel}:{ts}",
                    message_id=ts,
                    link=link,
                    at=datetime.now(UTC),
                )
        if not job_payload(row).files:
            return DeliveryOutcome(status="delivered")

        async def may_post() -> bool:
            again = await cleared(row)
            return not isinstance(again, str) and await refusal(client, again, ()) is None

        async def notice(words: str) -> None:
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                text=escape_mrkdwn_preserving_mentions(words), **place
            )

        await deliver_routine_files(
            runtime.anthropic,
            files_envelope(row),
            max_bytes=SLACK_MAX_BYTES_PER_FILE,
            post=build_output_poster(client, channel_id=channel, thread_ts=thread),
            notice=notice,
            may_post=may_post,
        )
        return DeliveryOutcome(status="delivered")

    return _post
