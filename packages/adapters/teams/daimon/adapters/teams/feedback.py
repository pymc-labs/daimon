"""Teams' built-in 👍/👎 on answers, stored like Slack and Discord votes.

The last answer chunk enables Teams' feedback loop. A click opens Teams' own
form and arrives as one invoke carrying the reaction and any text, so the
vote and its text are written together. Identity handling mirrors Slack: the
principal lookup never creates one, and the session is an attribution hint.
"""

from __future__ import annotations

import json
from typing import cast

import structlog
from daimon.adapters.teams.identity import canonical_uuid, live_tenant_id
from daimon.core.message_feedback import Vote
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.message_feedback import attach_feedback_text, record_vote
from daimon.core.stores.thread_sessions import get_latest_thread_session
from microsoft_teams.api import MessageSubmitActionInvokeActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

MAX_FEEDBACK_CHARS = 4000  # Slack's feedback modal cap.


def feedback_text(raw: str) -> str:
    """The typed text. Teams sends it as JSON `{"feedbackText": ...}`."""
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return raw.strip()[:MAX_FEEDBACK_CHARS]
    if isinstance(parsed, dict):
        text = cast(dict[str, object], parsed).get("feedbackText")
        return text.strip()[:MAX_FEEDBACK_CHARS] if isinstance(text, str) else ""
    return ""


async def record_feedback(
    sessionmaker: async_sessionmaker[AsyncSession],
    activity: MessageSubmitActionInvokeActivity,
    *,
    configured_tenant: str,
) -> None:
    """Record one feedback submission from the configured, live organisation."""
    conversation = activity.conversation
    user_id = canonical_uuid(activity.from_.aad_object_id)
    message_id = activity.reply_to_id
    if (
        user_id is None
        or not message_id
        or canonical_uuid(conversation.tenant_id) != configured_tenant
    ):
        log.info("teams.feedback.dropped")
        return
    vote: Vote = "up" if activity.value.action_value.reaction == "like" else "down"
    text = feedback_text(activity.value.action_value.feedback)
    tenant_id = await live_tenant_id(sessionmaker, configured_tenant)
    if tenant_id is None:
        log.info("teams.feedback.dropped")
        return
    async with sessionmaker.begin() as session:
        thread = await get_latest_thread_session(
            session, tenant_id=tenant_id, platform="teams", thread_id=conversation.id
        )
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=user_id
        )
        result = await record_vote(
            session,
            tenant_id=tenant_id,
            platform="teams",
            message_id=message_id,
            channel_id=conversation.id.split(";", 1)[0],
            platform_user_id=user_id,
            account_id=None if principal is None else principal.account_id,
            ma_session_id=None if thread is None else thread.ma_session_id,
            vote=vote,
        )
        if text:
            await attach_feedback_text(
                session, feedback_id=result.row.id, platform_user_id=user_id, feedback_text=text
            )
    log.info("feedback.vote_recorded", message_id=message_id, vote=vote, has_text=bool(text))
