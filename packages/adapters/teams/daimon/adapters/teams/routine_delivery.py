"""Teams' `RoutinePoster`: post a routine's result to its channel or thread.

Run by the app's delivery poller (`daimon.core.routine_delivery`) for the
configured organisation's routines only. A thread destination is
`<channel>;messageid=<root>`; its channel is what the access policy checks.
The routine posts on its creator's behalf, so the creator must still be on the
channel's roster, the rule the channel tools apply.

When the destination cannot be used (protected, gone, the creator no longer
in it, or Teams refuses the post) the result goes to the creator's 1:1 chat
instead, if the tenant's direct-message policy allows them. The creator is
cleared before any of this: an unreadable policy or a creator no longer allowed
to invoke the agent gets nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import httpx
import structlog
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize
from daimon.core.config import DirectMessagePolicy
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    clear_creator,
    delivery_target,
    render_fallback_dm,
    render_fallback_post,
    teams_thread_id,
)
from daimon.core.stores.domain import RoutineRow
from daimon.core.teams_bot_framework import SERVICE_URL
from microsoft_teams.api import Account, MessageActivityInput
from microsoft_teams.api.clients.conversation import CreateConversationParams
from microsoft_teams.apps import App
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["RoutineTeams", "SdkRoutineTeams", "make_teams_routine_poster"]

log = structlog.get_logger(__name__)


class RoutineTeams(Protocol):
    """The Bot Framework calls a routine post needs."""

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        """The person's roster id (`29:…`) in the conversation, or None when absent."""
        ...

    async def open_chat(self, member_id: str) -> str:
        """The bot's 1:1 chat with a roster member."""
        ...

    async def post(self, conversation_id: str, text: str) -> None: ...


class SdkRoutineTeams:
    """`RoutineTeams` over the SDK app, sending through the adapter's timed sender."""

    def __init__(self, app: App, sender: TeamsSender, *, entra_tenant_id: str) -> None:
        self._app = app
        self._sender = sender
        self._entra_tenant_id = entra_tenant_id

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        conversations = self._app.api.from_service_url(SERVICE_URL).conversations
        try:
            account = await conversations.get_member_by_id(conversation_id, aad_object_id)
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (403, 404):
                return None
            raise
        return account.id or None

    async def open_chat(self, member_id: str) -> str:
        params = CreateConversationParams(
            members=[Account(id=member_id)],
            tenant_id=self._entra_tenant_id,
            channel_data={"tenant": {"id": self._entra_tenant_id}},
        )
        conversations = self._app.api.from_service_url(SERVICE_URL).conversations
        return (await conversations.create(params)).id

    async def post(self, conversation_id: str, text: str) -> None:
        activity = MessageActivityInput(text=text, text_format="markdown").add_ai_generated()
        await self._sender.send(conversation_id, activity, service_url=SERVICE_URL)


def make_teams_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: RoutineTeams,
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
            await teams.post(await teams.open_chat(member), render_fallback_dm(row, reason))
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
        target = delivery_target(row, platform="teams")
        if target is None:
            return await dm_fallback(row, "destination_unavailable")
        if not authorize(
            cleared,
            subject=Subject(),
            action=Action.POST,
            surface=Surface.ROUTINE,
            place=Place(channel_id=target.channel_id),
        ):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason="protected_channel")
            return await dm_fallback(row, "protected_channel")
        creator = row.created_by_user_id
        try:
            on_roster = creator is not None and await teams.member(target.channel_id, creator)
        except TEAMS_SEND_ERRORS:
            return await dm_fallback(row, "destination_unavailable")
        if not on_roster:
            log.info(
                "routine.delivery_refused", routine_id=str(row.id), reason="creator_cannot_post"
            )
            return await dm_fallback(row, "creator_cannot_post")
        try:
            await teams.post(teams_thread_id(target), render_fallback_post(row))
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (400, 403, 404):
                return await dm_fallback(row, "destination_unavailable")
            raise
        return DeliveryOutcome(status="delivered")

    return post
