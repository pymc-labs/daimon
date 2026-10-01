"""Slack's `RoutinePoster`: post a routine's result to its destination.

Run by the app's delivery poller (`daimon.core.routine_delivery`). The web
client is built for the routine's own, live workspace (the tenant's team id),
so a destination can only ever be a channel in that workspace. A Slack thread
destination is `<channel id>:<thread ts>`; the thread's channel is what the
access policy checks. The text is escaped the way agent replies are, so a
routine can mention people but never broadcast (`<!channel>`, `<!here>`).

When the destination cannot be used — protected, or Slack refuses the post —
the result goes to the routine's creator by direct message instead, if the
tenant's direct-message policy allows them and they are still an active human
member of the workspace (the same rule as the direct-message tool). The creator
is checked before any of this: an unreadable policy or a creator no longer
allowed to invoke the agent gets nothing — no post and no direct message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

import structlog
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import TenantAccessPolicy, is_write_protected
from daimon.core.config import DirectMessagePolicy
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    DeliveryTarget,
    clear_creator,
    delivery_target,
    render_fallback_dm,
    render_fallback_post,
    slack_creator_may_post,
)
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.tenants import get_tenant
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["make_slack_routine_poster"]

log = structlog.get_logger(__name__)

#: Slack errors that mean the destination itself is unusable (not a transient
#: failure): the result goes to the creator by DM instead.
_UNUSABLE_DESTINATION = frozenset(
    {"channel_not_found", "not_in_channel", "is_archived", "thread_not_found"}
)


async def _dm_fallback(
    client: AsyncWebClient,
    row: RoutineRow,
    reason: str,
    *,
    team_id: str,
    policy: DirectMessagePolicy,
) -> DeliveryOutcome:
    creator = row.created_by_user_id
    if creator is None or not policy.allows(creator):
        return DeliveryOutcome(status="skipped", note=reason)
    try:
        info = await client.users_info(user=creator)  # pyright: ignore[reportUnknownMemberType]
        user = cast("dict[str, object]", info["user"])
        if (
            user.get("team_id") != team_id
            or user.get("deleted")
            or user.get("is_bot")
            or user.get("is_stranger")
        ):
            return DeliveryOutcome(status="skipped", note=reason)
        opened = await client.conversations_open(users=creator)  # pyright: ignore[reportUnknownMemberType]
        channel = cast("dict[str, str]", opened["channel"])["id"]
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel, text=escape_mrkdwn_preserving_mentions(render_fallback_dm(row, reason))
        )
    except SlackApiError as err:
        log.info("routine.delivery_dm_failed", routine_id=str(row.id), error=str(err))
        return DeliveryOutcome(status="skipped", note=reason)
    return DeliveryOutcome(status="delivered", note=f"dm_fallback:{reason}")


async def _is_member(client: AsyncWebClient, *, channel_id: str, user_id: str) -> bool:
    cursor: str | None = None
    while True:
        resp = await client.conversations_members(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id, limit=200, cursor=cursor
        )
        if user_id in cast("list[str]", resp["members"]):
            return True
        metadata = cast("dict[str, object]", resp.get("response_metadata") or {})  # pyright: ignore[reportUnknownMemberType]
        cursor = str(metadata.get("next_cursor") or "") or None
        if cursor is None:
            return False


async def _destination_refusal(
    client: AsyncWebClient, row: RoutineRow, target: DeliveryTarget
) -> str | None:
    """Why the destination cannot take this post now, or None.

    Re-checked at every post, because access and threads change after the
    routine is saved: the channel must still exist, the creator must still be
    allowed to post there (the channel tools' rule: private channel or guest
    → membership), and a stored thread must still exist — Slack accepts a
    missing `thread_ts` and posts at the channel root, so the ordinary
    `send_message` checks `conversations.replies` first, and so does this.
    """
    creator = row.created_by_user_id
    if creator is None:
        return "creator_cannot_post"
    try:
        info = await client.conversations_info(channel=target.channel_id)  # pyright: ignore[reportUnknownMemberType]
        channel = cast("dict[str, object]", info["channel"])
        if channel.get("is_archived"):
            return "destination_unavailable"
        user_info = await client.users_info(user=creator)  # pyright: ignore[reportUnknownMemberType]
        user = cast("dict[str, object]", user_info["user"])
        is_guest = bool(user.get("is_restricted") or user.get("is_ultra_restricted"))
        is_private = bool(channel.get("is_private"))
        is_member = (
            await _is_member(client, channel_id=target.channel_id, user_id=creator)
            if is_private or is_guest
            else False
        )
        if not slack_creator_may_post(
            is_im_or_mpim=bool(channel.get("is_im") or channel.get("is_mpim")),
            is_private=is_private,
            is_guest=is_guest,
            is_member=is_member,
        ):
            return "creator_cannot_post"
        if target.thread_ts is not None:
            replies = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
                channel=target.channel_id, ts=target.thread_ts, limit=1
            )
            messages = cast("list[object]", replies.get("messages") or [])  # pyright: ignore[reportUnknownMemberType]
            if not messages:
                return "destination_unavailable"
    except SlackApiError as err:
        error = str(cast("dict[str, object]", err.response.data).get("error", ""))  # pyright: ignore[reportUnknownMemberType]
        if error in _UNUSABLE_DESTINATION or error == "message_not_found":
            return "destination_unavailable"
        raise
    return None


def make_slack_routine_poster(
    runtime: SlackRuntime,
) -> Callable[[RoutineRow], Awaitable[DeliveryOutcome]]:
    async def _post(row: RoutineRow) -> DeliveryOutcome:
        async with runtime.sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        client = await resolve_web_client(runtime, team_id=tenant.external_id)
        if client is None:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        dm_policy = runtime.settings.direct_message_policies.get(
            row.tenant_id, DirectMessagePolicy()
        )
        # Creator first, before resolving or sending anything: a creator who
        # may no longer invoke the agent, or an unreadable policy, gets
        # nothing — no destination post and no direct message.
        cleared = await clear_creator(runtime.sessionmaker, row, platform="slack")
        if not isinstance(cleared, TenantAccessPolicy):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=cleared)
            return DeliveryOutcome(status="skipped", note=cleared)
        target = delivery_target(row, platform="slack")
        if target is None:
            return await _dm_fallback(
                client, row, "destination_unavailable", team_id=tenant.external_id, policy=dm_policy
            )
        if is_write_protected(cleared, channel_id=target.channel_id):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason="protected_channel")
            return await _dm_fallback(
                client, row, "protected_channel", team_id=tenant.external_id, policy=dm_policy
            )
        refusal = await _destination_refusal(client, row, target)
        if refusal is not None:
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=refusal)
            return await _dm_fallback(
                client, row, refusal, team_id=tenant.external_id, policy=dm_policy
            )
        text = escape_mrkdwn_preserving_mentions(render_fallback_post(row))
        try:
            if target.thread_ts is not None:
                await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=target.channel_id, thread_ts=target.thread_ts, text=text
                )
            else:
                await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=target.channel_id, text=text
                )
        except SlackApiError as err:
            error = str(cast("dict[str, object]", err.response.data).get("error", ""))  # pyright: ignore[reportUnknownMemberType]
            if error in _UNUSABLE_DESTINATION:
                return await _dm_fallback(
                    client,
                    row,
                    "destination_unavailable",
                    team_id=tenant.external_id,
                    policy=dm_policy,
                )
            raise
        return DeliveryOutcome(status="delivered")

    return _post
