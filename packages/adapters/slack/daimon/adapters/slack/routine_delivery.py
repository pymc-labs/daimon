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
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.output_delivery import SLACK_MAX_BYTES_PER_FILE, build_output_poster
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize
from daimon.core.config import DirectMessagePolicy
from daimon.core.message_split import split_fenced
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    DeliveryTarget,
    claim_still_current,
    clear_creator,
    delivery_target,
    render_fallback_dm,
    render_fallback_post,
    resolve_routine_identity,
    slack_creator_may_post,
    tenant_dm_still_open,
)
from daimon.core.routine_files import deliver_routine_files, payload_for
from daimon.core.routine_hold import result_is_held
from daimon.core.rule_views import keeps_routine_inside
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import get_routine
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
    output_client: AsyncAnthropic,
    may_post: Callable[[], Awaitable[bool]],
) -> DeliveryOutcome:
    creator = row.created_by_user_id
    if creator is None or not policy.allows(creator):
        return DeliveryOutcome(status="skipped", note=reason)
    try:
        if not await may_post():
            return DeliveryOutcome(status="skipped", note="Fallback DM access changed.")
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
        text = escape_mrkdwn_preserving_mentions(render_fallback_dm(row, reason, platform="slack"))
        for chunk in split_fenced(text, 11800, word_boundary=True):
            if not await may_post():
                return DeliveryOutcome(status="skipped", note="Fallback DM access changed.")
            await client.chat_postMessage(channel=channel, text=chunk)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown

        async def notice(text: str) -> None:
            await client.chat_postMessage(channel=channel, text=text)  # pyright: ignore[reportUnknownMemberType]

        await deliver_routine_files(
            output_client,
            payload_for(row),
            max_bytes=SLACK_MAX_BYTES_PER_FILE,
            post=build_output_poster(client, channel_id=channel, thread_ts=None),
            notice=notice,
            may_post=may_post,
        )
    except SlackApiError as err:
        log.info(
            "routine.delivery_dm_failed", routine_id=str(row.id), error_type=type(err).__name__
        )
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
        if row.destination_kind == "dm":
            return DeliveryOutcome(status="skipped", note="DM routines are not supported here.")
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
        # An isolated or shared channel's result never leaves it, not even by DM.
        keep_inside = keeps_routine_inside(cleared, row) or await result_is_held(
            runtime.sessionmaker, row, platform="slack"
        )

        async def creator_current() -> bool:
            async with runtime.sessionmaker() as session:
                current = await get_routine(session, row.id, tenant_id=row.tenant_id)
            if current is None or (current.destination_kind, current.destination_id) != (
                row.destination_kind,
                row.destination_id,
            ):
                return False
            if not claim_still_current(current, row, datetime.now(UTC)):
                return False
            return isinstance(
                await clear_creator(runtime.sessionmaker, row, platform="slack"), TenantAccessPolicy
            )

        async def fallback_guard() -> bool:
            if not await creator_current():
                return False
            if not await tenant_dm_still_open(runtime.sessionmaker, row, tenant.external_id):
                return False
            creator = row.created_by_user_id
            current_dm_policy = runtime.settings.direct_message_policies.get(
                row.tenant_id, DirectMessagePolicy()
            )
            if creator is None or not current_dm_policy.allows(creator):
                return False
            current_policy = await clear_creator(runtime.sessionmaker, row, platform="slack")
            if not isinstance(current_policy, TenantAccessPolicy) or keeps_routine_inside(
                current_policy, row
            ):
                return False
            info = await client.users_info(user=creator)  # pyright: ignore[reportUnknownMemberType]
            user = cast("dict[str, object]", info["user"])
            return (
                user.get("team_id") == tenant.external_id
                and not user.get("deleted")
                and not user.get("is_bot")
                and not user.get("is_stranger")
            )

        async def fallback(reason: str) -> DeliveryOutcome:
            if keep_inside:
                return DeliveryOutcome(status="skipped", note=reason)
            return await _dm_fallback(
                client,
                row,
                reason,
                team_id=tenant.external_id,
                policy=dm_policy,
                output_client=runtime.anthropic,
                may_post=fallback_guard,
            )

        target = delivery_target(row, platform="slack")
        if target is None:
            return await fallback("destination_unavailable")
        if not authorize(
            cleared,
            subject=Subject(),
            action=Action.POST,
            surface=Surface.ROUTINE,
            place=Place(channel_id=target.channel_id),
        ):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason="protected_channel")
            return await fallback("protected_channel")
        refusal = await _destination_refusal(client, row, target)
        if refusal is not None:
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=refusal)
            return await fallback(refusal)
        identity = await resolve_routine_identity(
            runtime.sessionmaker,
            runtime.anthropic,
            runtime.settings,
            row=row,
            platform="slack",
            workspace_id=tenant.external_id,
            default_agent_name=runtime.deployment_default.agent_name,
        )
        place: dict[str, Any] = (
            {"channel": target.channel_id, "thread_ts": target.thread_ts}
            if target.thread_ts is not None
            else {"channel": target.channel_id}
        )
        try:
            # Slack drops `username` and `icon_url` without an error when the
            # install lacks chat:write.customize, so the line keeps naming the
            # agent here; only Discord, whose label always carries it, drops it.
            text = escape_mrkdwn_preserving_mentions(render_fallback_post(row, platform="slack"))
            for chunk in split_fenced(text, 11800, word_boundary=True):
                await post_as_agent(client, identity, text=chunk, **place)
        except SlackApiError as err:
            error = str(cast("dict[str, object]", err.response.data).get("error", ""))  # pyright: ignore[reportUnknownMemberType]
            if error in _UNUSABLE_DESTINATION:
                return await fallback("destination_unavailable")
            raise

        async def may_post() -> bool:
            if not await creator_current():
                return False
            policy = await clear_creator(runtime.sessionmaker, row, platform="slack")
            return bool(
                isinstance(policy, TenantAccessPolicy)
                and authorize(
                    policy,
                    subject=Subject(),
                    action=Action.POST,
                    surface=Surface.ROUTINE,
                    place=Place(channel_id=target.channel_id),
                )
                and await _destination_refusal(client, row, target) is None
            )

        async def notice(text: str) -> None:
            await post_as_agent(
                client, identity, text=escape_mrkdwn_preserving_mentions(text), **place
            )

        await deliver_routine_files(
            runtime.anthropic,
            payload_for(row),
            max_bytes=SLACK_MAX_BYTES_PER_FILE,
            post=build_output_poster(
                client, channel_id=target.channel_id, thread_ts=target.thread_ts
            ),
            notice=notice,
            may_post=may_post,
        )
        return DeliveryOutcome(status="delivered")

    return _post
