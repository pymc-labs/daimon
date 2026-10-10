"""Teams' `RoutinePoster`: post a routine's result to its channel or thread.

Run by the app's delivery poller (`daimon.core.routine_delivery`) for the
configured organisation's routines only. A thread destination is
`<channel>;messageid=<root>`; its channel is what the access policy checks.
The routine posts on its creator's behalf, so the creator must still be on the
channel's roster, the rule the channel tools apply.

When the destination cannot be used (protected, gone, the creator no longer
in it, or Teams refuses the post) the result goes to the creator's 1:1 chat
instead, if the tenant's direct-message policy allows them, unless the
destination lies in an isolated channel, whose results never leave it. The creator is
cleared before any of this: an unreadable policy or a creator no longer allowed
to invoke the agent gets nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping

import httpx
import structlog
from daimon.adapters.teams.card import TEAMS_LIMIT
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize
from daimon.core.config import DirectMessagePolicy
from daimon.core.message_split import split_fenced
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    clear_creator,
    delivery_target,
    render_fallback_dm,
    render_fallback_post,
    teams_thread_id,
)
from daimon.core.rule_views import keeps_routine_inside
from daimon.core.stores.domain import RoutineRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["make_teams_routine_poster"]

log = structlog.get_logger(__name__)


def make_teams_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: DirectChats,
    *,
    tenant_id: uuid.UUID,
    dm_policies: Mapping[uuid.UUID, DirectMessagePolicy],
) -> Callable[[RoutineRow], Awaitable[DeliveryOutcome]]:
    """The poster for `tenant_id`'s routines; another organisation's rows are skipped."""

    async def dm_fallback(row: RoutineRow, reason: str) -> DeliveryOutcome:
        creator = row.created_by_user_id
        policy = dm_policies.get(row.tenant_id, DirectMessagePolicy())
        if creator is None or not policy.allows(creator):
            return DeliveryOutcome(status="skipped", note=reason)
        target = delivery_target(row, platform="teams")
        try:
            # The creator's roster id comes from the team the routine posts in.
            member = await teams.member(target.channel_id, creator) if target else None
            if member is None:
                return DeliveryOutcome(status="skipped", note=reason)
            chat = await teams.open_chat(member)
            for chunk in split_fenced(
                render_fallback_dm(row, reason), TEAMS_LIMIT, word_boundary=True
            ):
                await teams.post(chat, chunk)
        except TEAMS_SEND_ERRORS as err:
            log.info("routine.delivery_dm_failed", routine_id=str(row.id), error=type(err).__name__)
            return DeliveryOutcome(status="skipped", note=reason)
        return DeliveryOutcome(status="delivered", note=f"dm_fallback:{reason}")

    async def post(row: RoutineRow) -> DeliveryOutcome:
        if row.tenant_id != tenant_id:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        cleared = await clear_creator(sessionmaker, row, platform="teams")
        if not isinstance(cleared, TenantAccessPolicy):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=cleared)
            return DeliveryOutcome(status="skipped", note=cleared)
        keep_inside = keeps_routine_inside(cleared, row)

        async def fallback(reason: str) -> DeliveryOutcome:
            if keep_inside:  # an isolated channel's result never leaves it, not even by DM
                return DeliveryOutcome(status="skipped", note=reason)
            return await dm_fallback(row, reason)

        target = delivery_target(row, platform="teams")
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
        creator = row.created_by_user_id
        try:
            on_roster = creator is not None and await teams.member(target.channel_id, creator)
        except TEAMS_SEND_ERRORS:
            return await fallback("destination_unavailable")
        if not on_roster:
            log.info(
                "routine.delivery_refused", routine_id=str(row.id), reason="creator_cannot_post"
            )
            return await fallback("creator_cannot_post")
        try:
            for chunk in split_fenced(render_fallback_post(row), TEAMS_LIMIT, word_boundary=True):
                await teams.post(teams_thread_id(target), chunk)
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (400, 403, 404):
                return await fallback("destination_unavailable")
            raise
        return DeliveryOutcome(status="delivered")

    return post
