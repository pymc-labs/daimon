"""Slack's `Poster`: post a routine's result (a `DeliveryJob`) to its destination.

Run by the app's delivery poller (`daimon.core.routine_delivery`). The web
client is built for the routine's own, live workspace (the tenant's team id),
so a destination can only ever be a channel in that workspace. A Slack thread
destination is `<channel id>:<thread ts>`; the thread's channel is what the
access policy checks. The text is escaped the way agent replies are, so a
routine can mention people but never broadcast (`<!channel>`, `<!here>`).

When the destination cannot be used — protected, or Slack refuses the post —
the result goes to the routine's creator by direct message instead, if the
tenant's direct-message policy and the routine agent's DM grant allow them and
they are still an active human member of the workspace (the same rule as the
direct-message tool). The creator
is checked before any of this: an unreadable policy or a creator no longer
allowed to invoke the agent gets nothing — no post and no direct message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.adapters.slack.blockkit import error_title_block
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.output_delivery import SLACK_MAX_BYTES_PER_FILE, build_output_poster
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.user_groups import user_group_lookup
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize
from daimon.core.config import DirectMessagePolicy, env_dm_policy
from daimon.core.delivery import RoutinePoster
from daimon.core.dm_access import sender_admits
from daimon.core.fire.delivery import DeliveryJob, Poster, routine_poster
from daimon.core.fire.subject import RoutineFacts
from daimon.core.message_split import split_fenced
from daimon.core.outbound.opt_out import untag_opted_out
from daimon.core.owner_notices import OwnerDm, OwnerNotice
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    DeliveryTarget,
    clear_creator,
    delivery_target,
    render_fallback_dm,
    render_fallback_post,
    resolve_routine_identity,
    routine_agent_name,
    routine_requester_name,
    tenant_dm_still_open,
)
from daimon.core.routine_destinations import slack_may_deliver
from daimon.core.routine_files import deliver_routine_files, payload_for
from daimon.core.routine_hold import result_is_held
from daimon.core.rule_views import keeps_routine_inside
from daimon.core.stores.tenants import get_tenant
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["make_slack_owner_dm", "make_slack_poster", "make_slack_routine_poster"]

log = structlog.get_logger(__name__)

#: Slack errors that mean the destination itself is unusable (not a transient
#: failure): the result goes to the creator by DM instead.
_UNUSABLE_DESTINATION = frozenset(
    {"channel_not_found", "not_in_channel", "is_archived", "thread_not_found"}
)
#: Slack errors that mean the creator cannot get a DM at all, on every run alike.
_UNREACHABLE = frozenset({"user_not_found", "user_disabled", "account_inactive", "cannot_dm_bot"})


async def _dm_fallback(
    client: AsyncWebClient,
    job: DeliveryJob,
    reason: str,
    *,
    team_id: str,
    policy: DirectMessagePolicy,
    output_client: AsyncAnthropic,
    may_post: Callable[[], Awaitable[bool]],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> DeliveryOutcome:
    creator = job.created_by_user_id
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
        text = escape_mrkdwn_preserving_mentions(
            await untag_opted_out(
                sessionmaker,
                tenant_id=job.tenant_id,
                platform="slack",
                text=render_fallback_dm(job, reason, await routine_agent_name(sessionmaker, job)),
            )
        )
        for chunk in split_fenced(text, 11800, word_boundary=True):
            if not await may_post():
                return DeliveryOutcome(status="skipped", note="Fallback DM access changed.")
            await client.chat_postMessage(channel=channel, text=chunk)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown

        async def notice(text: str) -> None:
            await client.chat_postMessage(channel=channel, text=text)  # pyright: ignore[reportUnknownMemberType]

        await deliver_routine_files(
            output_client,
            payload_for(job),
            max_bytes=SLACK_MAX_BYTES_PER_FILE,
            post=build_output_poster(client, channel_id=channel, thread_ts=None),
            notice=notice,
            may_post=may_post,
            sessions=sessionmaker,
            tenant_id=job.tenant_id,
        )
    except SlackApiError as err:
        log.warning(
            "routine.delivery_fallback_failed",
            routine_id=str(job.id),
            error_type=type(err).__name__,
        )
        # A person gone or a DM Slack never allows repeats on every run: a skip, not a page.
        closed = str(err.response.get("error", "")) in _UNREACHABLE  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        return DeliveryOutcome(
            status="skipped", note=reason, failed=None if closed else type(err).__name__
        )
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


async def destination_refusal(
    client: AsyncWebClient, creator: str | None, target: DeliveryTarget
) -> str | None:
    """Why the destination cannot take this post now, or None.

    Re-checked at every post, because access and threads change after the
    routine is saved. `slack_may_deliver` decides, the rule the chat tools and
    the web app apply when the routine is saved: daimon still in a channel that
    is not archived, and the creator still allowed to post there (private
    channel or guest → membership). A stored thread must still exist too —
    Slack accepts a missing `thread_ts` and posts at the channel root, so the
    ordinary `send_message` checks `conversations.replies` first, and so does this.
    """
    if creator is None:
        return "creator_cannot_post"
    try:
        info = await client.conversations_info(channel=target.channel_id)  # pyright: ignore[reportUnknownMemberType]
        channel = cast("dict[str, object]", info["channel"])
        is_archived = bool(channel.get("is_archived"))
        bot_is_member = bool(channel.get("is_member"))
        is_im_or_mpim = bool(channel.get("is_im") or channel.get("is_mpim"))
        is_private = bool(channel.get("is_private"))
        is_guest = False
        is_member = False
        if bot_is_member and not is_archived and not is_im_or_mpim:
            user_info = await client.users_info(user=creator)  # pyright: ignore[reportUnknownMemberType]
            user = cast("dict[str, object]", user_info["user"])
            is_guest = bool(user.get("is_restricted") or user.get("is_ultra_restricted"))
            if is_private or is_guest:
                is_member = await _is_member(client, channel_id=target.channel_id, user_id=creator)
        if not slack_may_deliver(
            bot_is_member=bot_is_member,
            is_archived=is_archived,
            is_im_or_mpim=is_im_or_mpim,
            is_private=is_private,
            is_guest=is_guest,
            caller_is_member=is_member,
        ):
            if not is_im_or_mpim and (is_archived or not bot_is_member):
                return "destination_unavailable"
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


def make_slack_owner_dm(runtime: SlackRuntime) -> OwnerDm:
    """DM a routine's owner its failure notice, under the rules of the result fallback DM."""

    async def send(row: RoutineFacts, notice: OwnerNotice) -> str | None:
        async with runtime.sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
        client = (
            await resolve_web_client(runtime, team_id=tenant.external_id)
            if tenant is not None and tenant.archived_at is None
            else None
        )
        if tenant is None or client is None:
            return "tenant_archived"
        owner = row.created_by_user_id
        policy = env_dm_policy(runtime.settings.direct_message_policies, row.tenant_id)
        if owner is None or not policy.allows(owner):
            return "dm_policy_denies"
        if not await tenant_dm_still_open(runtime.sessionmaker, row, tenant.external_id):
            return "dms_off"
        if not await sender_admits(
            runtime.sessionmaker,
            tenant_id=row.tenant_id,
            agent_name=row.agent_name,
            user_id=owner,
            groups=user_group_lookup(runtime, client, team_id=tenant.external_id),
        ):
            return "agent_closed"
        info = await client.users_info(user=owner)  # pyright: ignore[reportUnknownMemberType]
        user = cast("dict[str, object]", info["user"])
        if (
            user.get("team_id") != tenant.external_id
            or user.get("deleted")
            or user.get("is_bot")
            or user.get("is_stranger")
        ):
            return "person_unreachable"
        opened = await client.conversations_open(users=owner)  # pyright: ignore[reportUnknownMemberType]
        channel = cast("dict[str, str]", opened["channel"])["id"]
        if notice.text is not None:
            await client.chat_postMessage(channel=channel, text=notice.text)  # pyright: ignore[reportUnknownMemberType]
        else:
            # The body is unescaped: it carries Slack's own date markup.
            blocks = [
                error_title_block(notice.title),
                {"type": "section", "text": {"type": "mrkdwn", "text": notice.body}},
            ]
            await client.chat_postMessage(channel=channel, text=notice.title, blocks=blocks)  # pyright: ignore[reportUnknownMemberType]
        return None

    return send


def make_slack_routine_poster(runtime: SlackRuntime) -> RoutinePoster:
    """The routine outbox's poster, over `make_slack_poster`."""
    return routine_poster(
        runtime.sessionmaker,
        make_slack_poster(runtime),
        platform="slack",
        settings=runtime.settings,
    )


def make_slack_poster(runtime: SlackRuntime) -> Poster:
    """Post a job to its channel or thread, else by DM to its owner where the rules allow."""

    async def _post(job: DeliveryJob) -> DeliveryOutcome:
        if job.destination_kind == "dm":
            return DeliveryOutcome(status="skipped", note="DM routines are not supported here.")
        async with runtime.sessionmaker() as session:
            tenant = await get_tenant(session, job.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        client = await resolve_web_client(runtime, team_id=tenant.external_id)
        if client is None:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        dm_policy = env_dm_policy(runtime.settings.direct_message_policies, job.tenant_id)
        # Creator first, before resolving or sending anything: a creator who
        # may no longer invoke the agent, or an unreadable policy, gets
        # nothing — no destination post and no direct message.
        cleared = await clear_creator(runtime.sessionmaker, job, platform="slack")
        if not isinstance(cleared, TenantAccessPolicy):
            log.info("routine.delivery_refused", routine_id=str(job.id), reason=cleared)
            return DeliveryOutcome(status="skipped", note=cleared)
        # An isolated or shared channel's result never leaves it, not even by DM.
        keep_inside = keeps_routine_inside(cleared, job) or await result_is_held(
            runtime.sessionmaker, job, platform="slack"
        )

        async def creator_current() -> bool:
            if await job.still_current() is not None:
                return False
            return isinstance(
                await clear_creator(runtime.sessionmaker, job, platform="slack"), TenantAccessPolicy
            )

        async def fallback_guard() -> bool:
            if not await creator_current():
                return False
            if not await tenant_dm_still_open(runtime.sessionmaker, job, tenant.external_id):
                return False
            creator = job.created_by_user_id
            current_dm_policy = env_dm_policy(
                runtime.settings.direct_message_policies, job.tenant_id
            )
            if creator is None or not current_dm_policy.allows(creator):
                return False
            # The routine's agent grant must admit the creator; locks don't apply.
            if not await sender_admits(
                runtime.sessionmaker,
                tenant_id=job.tenant_id,
                agent_name=job.agent_name,
                user_id=creator,
                groups=user_group_lookup(runtime, client, team_id=tenant.external_id),
            ):
                return False
            current_policy = await clear_creator(runtime.sessionmaker, job, platform="slack")
            if not isinstance(current_policy, TenantAccessPolicy) or keeps_routine_inside(
                current_policy, job
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
                job,
                reason,
                team_id=tenant.external_id,
                policy=dm_policy,
                output_client=runtime.anthropic,
                may_post=fallback_guard,
                sessionmaker=runtime.sessionmaker,
            )

        target = delivery_target(job, platform="slack")
        if target is None:
            return await fallback("destination_unavailable")
        if not authorize(
            cleared,
            subject=Subject(),
            action=Action.POST,
            surface=Surface.ROUTINE,
            place=Place(channel_id=target.channel_id),
        ):
            log.info("routine.delivery_refused", routine_id=str(job.id), reason="protected_channel")
            return await fallback("protected_channel")
        refusal = await destination_refusal(client, job.created_by_user_id, target)
        if refusal is not None:
            log.info("routine.delivery_refused", routine_id=str(job.id), reason=refusal)
            return await fallback(refusal)
        identity = await resolve_routine_identity(
            runtime.sessionmaker,
            runtime.anthropic,
            runtime.settings,
            row=job,
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
            text = escape_mrkdwn_preserving_mentions(
                await untag_opted_out(
                    runtime.sessionmaker,
                    tenant_id=job.tenant_id,
                    platform="slack",
                    text=render_fallback_post(
                        job,
                        platform="slack",
                        requester_name=await routine_requester_name(
                            runtime.sessionmaker, job, "slack"
                        ),
                        agent_name=await routine_agent_name(runtime.sessionmaker, job),
                        header=job.header,
                    ),
                )
            )
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
            policy = await clear_creator(runtime.sessionmaker, job, platform="slack")
            return bool(
                isinstance(policy, TenantAccessPolicy)
                and authorize(
                    policy,
                    subject=Subject(),
                    action=Action.POST,
                    surface=Surface.ROUTINE,
                    place=Place(channel_id=target.channel_id),
                )
                and await destination_refusal(client, job.created_by_user_id, target) is None
            )

        async def notice(text: str) -> None:
            await post_as_agent(
                client, identity, text=escape_mrkdwn_preserving_mentions(text), **place
            )

        await deliver_routine_files(
            runtime.anthropic,
            payload_for(job),
            max_bytes=SLACK_MAX_BYTES_PER_FILE,
            post=build_output_poster(
                client, channel_id=target.channel_id, thread_ts=target.thread_ts
            ),
            notice=notice,
            may_post=may_post,
            sessions=runtime.sessionmaker,
            tenant_id=job.tenant_id,
        )
        return DeliveryOutcome(status="delivered")

    return _post
