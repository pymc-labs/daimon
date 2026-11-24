"""Teams' `Poster`: post a routine's result (a `DeliveryJob`) to its channel or thread.

Run by the app's delivery poller (`daimon.core.routine_delivery`) for the
configured organisation's routines only. A thread destination is
`<channel>;messageid=<root>`; its channel is what the access policy checks.
The routine posts on its creator's behalf, so the creator must still be on the
channel's roster, the rule the channel tools apply.

When the destination cannot be used (protected, gone, the creator no longer
in it, or Teams refuses the post) the result goes to the creator's 1:1 chat
instead, if the tenant's direct-message policy and the routine agent's DM
grant allow them, unless the
destination lies in an isolated channel, whose results never leave it. The creator is
cleared before any of this: an unreadable policy or a creator no longer allowed
to invoke the agent gets nothing.

A result posted in a channel also sends the creator an activity feed notice
linking to it (`activity_feed`), one entry per routine that each run
replaces; a 1:1 chat notifies on its own, so the fallback sends none.
Each message of a result is recorded as it is posted (`source='routine'` or `'workflow'`, the
creator as requester), so the routine's agent can tidy it. A post to a bare
channel roots its own thread and is keyed as that thread, as the tidy tools
and the agent's context name it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace

import httpx
import structlog
from daimon.adapters.teams.activity_feed import ActivityFeed, chain_id
from daimon.adapters.teams.card import TEAMS_LIMIT, notice_card
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, send_error_details
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize
from daimon.core.channel_tidy import record_turn_post
from daimon.core.config import DirectMessagePolicy, env_dm_policy
from daimon.core.delivery import RoutinePoster
from daimon.core.dm_access import groups_by_members, sender_admits
from daimon.core.fire.delivery import DeliveryJob, Poster, routine_poster
from daimon.core.fire.subject import RoutineFacts
from daimon.core.message_split import split_fenced
from daimon.core.native_admins import GroupMembers
from daimon.core.owner_notices import OwnerDm, OwnerNotice
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    clear_creator,
    delivery_target,
    render_fallback_dm,
    render_fallback_post,
    routine_agent_name,
    routine_requester_name,
    teams_thread_id,
)
from daimon.core.routine_files import payload_for, unsupported_files_note
from daimon.core.routine_hold import result_is_held
from daimon.core.rule_views import keeps_routine_inside
from daimon.core.stores.direct_messages import dm_enabled
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["make_teams_owner_dm", "make_teams_poster", "make_teams_routine_poster"]

log = structlog.get_logger(__name__)


def make_teams_owner_dm(
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: DirectChats,
    *,
    tenant_id: uuid.UUID,
    dm_policies: Mapping[uuid.UUID, DirectMessagePolicy],
    team_members: GroupMembers | None = None,
) -> OwnerDm:
    """DM a routine's owner its failure notice, under the rules of the result fallback DM."""

    async def send(row: RoutineFacts, notice: OwnerNotice) -> str | None:
        if row.tenant_id != tenant_id:
            return "other_organisation"
        owner = row.created_by_user_id
        async with sessionmaker() as session:
            enabled = await dm_enabled(session, tenant_id=row.tenant_id)
        if owner is None or not env_dm_policy(dm_policies, row.tenant_id).allows(owner):
            return "dm_policy_denies"
        if not enabled:
            return "dms_off"
        if not await sender_admits(
            sessionmaker,
            tenant_id=row.tenant_id,
            agent_name=row.agent_name,
            user_id=owner,
            groups=groups_by_members(team_members),
        ):
            return "agent_closed"
        # The owner's roster id comes from the team the routine posts in.
        target = delivery_target(row, platform="teams")
        member = await teams.member(target.channel_id, owner) if target else None
        if member is None:
            return "person_unreachable"
        chat = await teams.open_chat(member)
        if notice.text is not None:
            await teams.post(chat, notice.text)
        else:
            await teams.post_card(chat, notice_card(f"{notice.title}\n\n{notice.body}"))
        return None

    return send


def make_teams_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: DirectChats,
    *,
    tenant_id: uuid.UUID,
    dm_policies: Mapping[uuid.UUID, DirectMessagePolicy],
    feed: ActivityFeed | None = None,
    team_members: GroupMembers | None = None,
) -> RoutinePoster:
    """The routine outbox's poster for `tenant_id`, over `make_teams_poster`."""
    return routine_poster(
        sessionmaker,
        make_teams_poster(
            sessionmaker,
            teams,
            tenant_id=tenant_id,
            dm_policies=dm_policies,
            feed=feed,
            team_members=team_members,
        ),
        platform="teams",
    )


def make_teams_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: DirectChats,
    *,
    tenant_id: uuid.UUID,
    dm_policies: Mapping[uuid.UUID, DirectMessagePolicy],
    feed: ActivityFeed | None = None,
    team_members: GroupMembers | None = None,
) -> Poster:
    """The poster for `tenant_id`'s results; another organisation's jobs are skipped.

    `feed` sends the creator a notice for a result posted in a channel; None sends none.
    `team_members` resolves the teams a DM grant names; None leaves them unknown.
    """

    async def record(
        job: DeliveryJob,
        conversation: str,
        activity_id: str,
        *,
        delivery: uuid.UUID,
        parent: str | None = None,
    ) -> None:
        assert job.created_by_user_id is not None
        await record_turn_post(
            sessionmaker,
            tenant_id=job.tenant_id,
            platform="teams",
            ma_agent_id=job.agent_id,
            channel_id=conversation,
            message_id=activity_id,
            requester_platform_user_id=job.created_by_user_id,
            source=job.source,
            parent_channel_id=parent,
            role="routine",
            # Chunks in a channel each root their own thread; this keeps them one reply.
            delivery_id=delivery,
        )

    async def dm_fallback(job: DeliveryJob, reason: str) -> DeliveryOutcome:
        creator = job.created_by_user_id
        policy = env_dm_policy(dm_policies, job.tenant_id)
        async with sessionmaker() as session:
            enabled = await dm_enabled(session, tenant_id=job.tenant_id)
        if (
            not enabled
            or creator is None
            or not policy.allows(creator)
            or not await sender_admits(
                sessionmaker,
                tenant_id=job.tenant_id,
                agent_name=job.agent_name,
                user_id=creator,
                groups=groups_by_members(team_members),
            )
        ):
            return DeliveryOutcome(status="skipped", note=reason)
        target = delivery_target(job, platform="teams")
        try:
            # The creator's roster id comes from the team the routine posts in.
            member = await teams.member(target.channel_id, creator) if target else None
            if member is None:
                return DeliveryOutcome(status="skipped", note=reason)
            chat = await teams.open_chat(member)
            delivery = job.delivery_id or uuid.uuid4()
            for chunk in split_fenced(
                render_fallback_dm(job, reason, await routine_agent_name(sessionmaker, job))
                + unsupported_files_note(payload_for(job)),
                TEAMS_LIMIT,
                word_boundary=True,
            ):
                await record(job, chat, await teams.post(chat, chunk), delivery=delivery)
        except TEAMS_SEND_ERRORS as err:
            log.warning(
                "routine.delivery_fallback_failed",
                routine_id=str(job.id),
                error_type=type(err).__name__,
            )
            # The app blocked or removed, or the person gone, repeats on every run: no page.
            closed = send_error_details(err)[0] in {403, 404}
            return DeliveryOutcome(
                status="skipped", note=reason, failed=None if closed else type(err).__name__
            )
        return DeliveryOutcome(status="delivered", note=f"dm_fallback:{reason}")

    async def post(job: DeliveryJob) -> DeliveryOutcome:
        if job.destination_kind == "dm":
            return DeliveryOutcome(status="skipped", note="DM routines are not supported here.")
        if job.tenant_id != tenant_id:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        cleared = await clear_creator(sessionmaker, job, platform="teams")
        if not isinstance(cleared, TenantAccessPolicy):
            log.info("routine.delivery_refused", routine_id=str(job.id), reason=cleared)
            return DeliveryOutcome(status="skipped", note=cleared)
        # An isolated or shared channel's result never leaves it, not even by DM.
        held = await result_is_held(sessionmaker, job, platform="teams")
        keep_inside = keeps_routine_inside(cleared, job) or held

        async def fallback(reason: str) -> DeliveryOutcome:
            if keep_inside:
                return DeliveryOutcome(status="skipped", note=reason)
            return await dm_fallback(job, reason)

        target = delivery_target(job, platform="teams")
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
        creator = job.created_by_user_id
        try:
            on_roster = creator is not None and await teams.member(target.channel_id, creator)
        except TEAMS_SEND_ERRORS:
            return await fallback("destination_unavailable")
        if not on_roster:
            log.info(
                "routine.delivery_refused", routine_id=str(job.id), reason="creator_cannot_post"
            )
            return await fallback("creator_cannot_post")
        name = await routine_requester_name(sessionmaker, job, "teams")
        text = render_fallback_post(
            job,
            platform="teams",
            requester_name=name,
            agent_name=await routine_agent_name(sessionmaker, job),
            header=job.header,
        ) + unsupported_files_note(payload_for(job))
        conversation = teams_thread_id(target)
        ids: list[str] = []
        delivery = job.delivery_id or uuid.uuid4()
        try:
            for chunk in split_fenced(text, TEAMS_LIMIT, word_boundary=True):
                ids.append(activity_id := await teams.post(conversation, chunk))
                # A post to the channel roots its own thread.
                rooted = target if target.thread_ts else replace(target, thread_ts=activity_id)
                await record(
                    job,
                    teams_thread_id(rooted),
                    activity_id,
                    delivery=delivery,
                    parent=target.channel_id,
                )
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (400, 403, 404):
                return await fallback("destination_unavailable")
            raise
        # The activity-feed notice quotes the result to the creator outside the channel.
        if feed is not None and creator is not None and ids and not held:
            headline, _, body = text.partition("\n\n")
            feed.notify(
                conversation,
                ids[0],
                recipient_id=creator,
                text=headline.rstrip(":"),
                preview=body or headline,
                chain=chain_id(job.id),
            )
        return DeliveryOutcome(status="delivered")

    return post
