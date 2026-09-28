"""Teams' feedback buttons store a vote and its text."""

from __future__ import annotations

import pytest
from daimon.adapters.teams.feedback import feedback_text, record_feedback
from daimon.core._models import MessageFeedback
from microsoft_teams.api import MessageSubmitActionInvokeActivity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AAD_OBJECT_ID, BOT_ACCOUNT_ID, CHANNEL_ID, ENTRA_TENANT_ID, THREAD_ID


def _activity(
    reaction: str, feedback: str, *, tenant: str = ENTRA_TENANT_ID
) -> MessageSubmitActionInvokeActivity:
    return MessageSubmitActionInvokeActivity.model_validate(
        {
            "type": "invoke",
            "name": "message/submitAction",
            "id": "invoke-1",
            "channelId": "msteams",
            "serviceUrl": "https://smba.trafficmanager.net/test",
            "from": {"id": f"29:{AAD_OBJECT_ID}", "aadObjectId": AAD_OBJECT_ID},
            "recipient": {"id": BOT_ACCOUNT_ID},
            "conversation": {"id": THREAD_ID, "conversationType": "channel", "tenantId": tenant},
            "replyToId": "m-7",
            "value": {
                "actionName": "feedback",
                "actionValue": {"reaction": reaction, "feedback": feedback},
            },
        }
    )


def test_feedback_text_reads_teams_json_and_tolerates_plain_text() -> None:
    assert feedback_text('{"feedbackText": " too slow "}') == "too slow"
    assert feedback_text('{"other": 1}') == ""
    assert feedback_text("plain") == "plain"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_dislike_with_text_is_stored_against_the_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    activity = _activity("dislike", '{"feedbackText": "wrong prior"}')
    await record_feedback(db_session_factory, activity, configured_tenant=ENTRA_TENANT_ID)

    async with db_session_factory() as session:
        [row] = (await session.execute(select(MessageFeedback))).scalars().all()
    assert (row.platform, row.message_id, row.channel_id) == ("teams", "m-7", CHANNEL_ID)
    assert (row.vote, row.feedback_text, row.platform_user_id) == (
        "down",
        "wrong prior",
        AAD_OBJECT_ID,
    )


@pytest.mark.usefixtures("provisioned_tenant")
async def test_feedback_from_another_organisation_is_dropped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    other = "00000000-0000-0000-0000-000000000099"
    await record_feedback(
        db_session_factory, _activity("like", "{}", tenant=other), configured_tenant=ENTRA_TENANT_ID
    )
    async with db_session_factory() as session:
        assert (await session.execute(select(MessageFeedback))).scalars().all() == []
