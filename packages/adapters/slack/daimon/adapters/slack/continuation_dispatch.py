"""Slack I/O ports for the shared continuation and wake dispatcher."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.slack.context import THREAD_PAGE_LIMIT
from daimon.core.continuity.dispatch import dispatch_pending_continuations as dispatch_core
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.turn.protection import protection_state
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
__all__ = ["RunFollowUp", "dispatch_pending_continuations"]
RunFollowUp = Callable[[TaskContinuationRow, str], Awaitable[None]]


async def _latest_human_message_at(
    web_client: AsyncWebClient, *, channel: str, thread_ts: str, after: datetime
) -> datetime | None:
    """The newest human (non-bot) message timestamp after `after`, one page.

    Mirrors `context.build_delta_xml`'s `conversations.replies` call exactly
    (`oldest`, `inclusive=False`, `limit=THREAD_PAGE_LIMIT`): the dispatch
    decision must never depend on Slack history beyond what an ordinary turn
    would ever read.
    """
    response = await web_client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
        channel=channel,
        ts=thread_ts,
        oldest=f"{after.timestamp():.6f}",
        inclusive=False,
        limit=THREAD_PAGE_LIMIT,
    )
    messages = cast(list[dict[str, Any]], response["messages"])  # pyright: ignore[reportUnknownVariableType]
    latest: datetime | None = None
    for msg in messages:
        if "bot_id" in msg or not msg.get("user"):
            continue
        ts_value = cast(str | None, msg.get("ts"))
        if not ts_value:
            continue
        candidate = datetime.fromtimestamp(float(ts_value), tz=UTC)
        if latest is None or candidate > latest:
            latest = candidate
    return latest


async def dispatch_pending_continuations(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    web_client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    thread_id: str,
    active_turn: bool,
    run_follow_up: RunFollowUp,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Dispatch under the caller's guard, preserving its active-turn check."""

    async def latest(row: TaskContinuationRow) -> datetime | None:
        return await _latest_human_message_at(
            web_client, channel=channel, thread_ts=thread_id, after=row.created_at
        )

    async def context(row: TaskContinuationRow) -> tuple[datetime | None, bool]:
        return await latest(row), active_turn

    async def post(row: TaskContinuationRow, text: str, reason: str) -> None:
        # The access policy's may-post decision, asked right before each post:
        # a protected thread, or one whose protection can't be read, gets
        # nothing; the caller still settles the row.
        state = await protection_state(
            sessionmaker, tenant_id=tenant_id, channel_id=channel, thread_id=thread_id
        )
        if not state.may_post:
            log.info(
                "slack.continuation.notice_withheld",
                thread_id=thread_id,
                reason=reason,
                state=state.value,
            )
            return
        await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=channel, thread_ts=thread_id, text=text
        )

    await dispatch_core(
        sessionmaker,
        anthropic,
        tenant_id=tenant_id,
        platform="slack",
        thread_id=thread_id,
        run_follow_up=run_follow_up,
        decision_context=context,
        notice=post,
        settle_before_notice=False,
        notify_missing_seed=False,
        dispatch_errors=(SlackApiError,),
        now=now,
    )
