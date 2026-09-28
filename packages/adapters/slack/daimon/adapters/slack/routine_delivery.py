"""Slack's `RoutinePoster`: post a routine's result tail to its destination.

Run by the app's delivery poller (`daimon.core.routine_delivery`). The web
client is built for the routine's own workspace (the tenant's team id), so a
destination can only ever be a channel in that workspace. A Slack thread
destination is `<channel id>:<thread ts>`; the thread's channel is what the
access policy checks. The text is escaped the way agent replies are, so a
routine can mention people but never broadcast (`<!channel>`, `<!here>`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    check_delivery,
    delivery_target,
    render_fallback_post,
)
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.tenants import get_tenant

__all__ = ["make_slack_routine_poster"]

log = structlog.get_logger(__name__)


def make_slack_routine_poster(
    runtime: SlackRuntime,
) -> Callable[[RoutineRow], Awaitable[DeliveryOutcome]]:
    async def _post(row: RoutineRow) -> DeliveryOutcome:
        target = delivery_target(row, platform="slack")
        if target is None:
            return DeliveryOutcome(status="skipped", note="destination_unavailable")
        async with runtime.sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return DeliveryOutcome(status="skipped", note="destination_unavailable")
        client = await resolve_web_client(runtime, team_id=tenant.external_id)
        if client is None:
            return DeliveryOutcome(status="skipped", note="destination_unavailable")
        refusal = await check_delivery(runtime.sessionmaker, row, platform="slack", target=target)
        if refusal is not None:
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=refusal)
            return DeliveryOutcome(status="skipped", note=refusal)
        text = escape_mrkdwn_preserving_mentions(render_fallback_post(row))
        if target.thread_ts is not None:
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=target.channel_id, thread_ts=target.thread_ts, text=text
            )
        else:
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=target.channel_id, text=text
            )
        return DeliveryOutcome(status="delivered")

    return _post
