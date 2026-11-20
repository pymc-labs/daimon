"""Slack's `WorkflowPoster`: post a workflow's Finish result or owner notice.

Run by the app's delivery poller next to the routine poster, with a client for the
workflow's own, live workspace. The channel must take the post as it would the
speaker's own (`destination_refusal`, the routine rule), and a thread
(`<channel>:<ts>`) must still exist. The rules every workflow post passes come from
`daimon.core.workflows.delivery`. A notice is an error card; a result is text escaped
as agent replies are (people may be tagged, never `<!channel>`), then files.
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.blockkit import error_blocks
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.output_delivery import SLACK_MAX_BYTES_PER_FILE, build_output_poster
from daimon.adapters.slack.routine_delivery import destination_refusal
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.delivery import DeliveryOutcome, WorkflowPoster, files_envelope, job_payload
from daimon.core.message_split import split_fenced
from daimon.core.routine_delivery import DeliveryTarget
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
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["make_slack_workflow_poster"]


def _skipped(note: str) -> DeliveryOutcome:
    return DeliveryOutcome(status="skipped", note=note)


def make_slack_workflow_poster(runtime: SlackRuntime) -> WorkflowPoster:
    async def refusal(
        client: AsyncWebClient, post: WorkflowPost, mentioned: tuple[str, ...]
    ) -> str | None:
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
        return await clear_post(
            runtime.sessionmaker, row, platform="slack", web=runtime.settings.web
        )

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
        place: dict[str, Any] = {"channel": post.channel_id}
        if post.thread_id is not None:
            place["thread_ts"] = post.thread_id
        text = escape_mrkdwn_preserving_mentions(rendered.text)
        if post.notice:
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                text=text, blocks=error_blocks(NOTICE_TITLE, rendered.text), **place
            )
            return DeliveryOutcome(status="delivered")
        for chunk in split_fenced(text, 11800, word_boundary=True):
            await client.chat_postMessage(text=chunk, **place)  # pyright: ignore[reportUnknownMemberType]
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
            post=build_output_poster(client, channel_id=post.channel_id, thread_ts=post.thread_id),
            notice=notice,
            may_post=may_post,
        )
        return DeliveryOutcome(status="delivered")

    return _post
