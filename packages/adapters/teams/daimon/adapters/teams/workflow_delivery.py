"""Teams' `WorkflowPoster`: post a workflow's Finish result or owner notice.

Run by the app's delivery poller next to the routine poster, for the configured
organisation only. A thread is `<channel>;messageid=<root>`; its channel is what the
rules check. The speaker (the run-as person, or the owner for a notice) must still be
on the channel's roster, the rule the channel tools apply. The rules every workflow
post passes come from `daimon.core.workflows.delivery`. A notice is an error card.
Channel threads take no bot file uploads, so files are named as not supported here
(the same recorded gap as a routine's files).
"""

from __future__ import annotations

import uuid

import httpx
from daimon.adapters.teams.card import TEAMS_LIMIT, notice_card
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.core.config import Settings
from daimon.core.delivery import DeliveryOutcome, WorkflowPoster, files_envelope
from daimon.core.message_split import split_fenced
from daimon.core.routine_files import unsupported_files_note
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.workflows.delivery import (
    NOTICE_TITLE,
    clear_post,
    post_refusal,
    post_text,
    reach_limits,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["make_teams_workflow_poster"]


def _skipped(note: str) -> DeliveryOutcome:
    return DeliveryOutcome(status="skipped", note=note)


def make_teams_workflow_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: DirectChats,
    *,
    tenant_id: uuid.UUID,
    settings: Settings,
) -> WorkflowPoster:
    async def post(row: DeliveryRow) -> DeliveryOutcome:
        if row.tenant_id != tenant_id:
            return _skipped("tenant_archived")
        cleared = await clear_post(sessionmaker, row, platform="teams", web=settings.web)
        if isinstance(cleared, str):
            return _skipped(cleared)
        try:
            on_roster = await teams.member(cleared.channel_id, cleared.speaker)
        except TEAMS_SEND_ERRORS:
            return _skipped("destination_unavailable")
        if not on_roster:
            return _skipped("creator_cannot_post")
        rendered = await post_text(sessionmaker, cleared, "teams")
        refused = await post_refusal(
            cleared, place_id=cleared.channel_id, limits=reach_limits(settings)
        )
        if refused is not None:
            return _skipped(refused)
        conversation = (
            cleared.channel_id
            if cleared.thread_id is None
            else f"{cleared.channel_id};messageid={cleared.thread_id}"
        )
        try:
            if cleared.notice:
                card = notice_card(f"{NOTICE_TITLE}\n\n{rendered.text}", error=True)
                await teams.post_card(conversation, card)
                return DeliveryOutcome(status="delivered")
            text = rendered.text + unsupported_files_note(files_envelope(row))
            for chunk in split_fenced(text, TEAMS_LIMIT, word_boundary=True):
                await teams.post(conversation, chunk)
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (400, 403, 404):
                return _skipped("destination_unavailable")
            raise
        return DeliveryOutcome(status="delivered")

    return post
