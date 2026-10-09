"""Teams' 👍/👎 through the real SDK routes: the vote, the "What went wrong?" form, who may
vote, and a tenant's forms routed to the support channel."""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
import structlog
from daimon.adapters.teams import feedback, support
from daimon.adapters.teams.answer_access import IN_DIRECT_CHAT, NOT_ALLOWED, AnswerPlace
from daimon.adapters.teams.feedback import FEEDBACK_DIALOG, feedback_text, form_details, sent_form
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.message_feedback import FEEDBACK_REASONS
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.tenants import get_tenant, set_provision_status
from daimon.testing.factories import make_thread_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    THREAD_ID,
    USER_NAME,
    TeamsApiFake,
    assert_card_renders,
    build_teams_runtime,
    make_invoke,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
OPS = "19:ops@thread.tacv2"
ANSWER = "m-7"  # `make_invoke`'s card message: the answer clicked on.
CRITICISM = "the prior is wrong"


def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake, *, routed: bool = False
) -> AbstractAsyncContextManager[TeamsHttpService]:
    runtime = build_teams_runtime(db_factory)
    runtime.settings.support = SupportSettings(
        teams_escalation_channel_id=OPS, feedback_to_support={TENANT: routed}
    )
    return running_service(runtime, fake)


async def _click(service: TeamsHttpService, reaction: str, *, user: str = AAD_OBJECT_ID) -> Any:
    value = {"data": {"actionName": "feedback", "actionValue": {"reaction": reaction}}}
    return await post_activity(service, make_invoke("message/fetchTask", value, user=user))


async def _send(
    service: TeamsHttpService,
    *,
    reasons: str = "",
    note: str = "",
    user: str = AAD_OBJECT_ID,
) -> Any:
    data = {"action": FEEDBACK_DIALOG, "message": ANSWER, "reasons": reasons, "text": note}
    reply = await post_activity(service, make_invoke("task/submit", {"data": data}, user=user))
    await _routed(service)
    return reply


async def _routed(service: TeamsHttpService) -> None:
    """Wait for a routed form's background post: tests share one database connection."""
    async with asyncio.timeout(10):
        while service.turns.in_flight:
            await asyncio.sleep(0.01)


async def _send_as_action(
    service: TeamsHttpService, *, reasons: str = "", note: str = "", **marker: str
) -> Any:
    """The form's Send as Teams' custom loop may deliver it: `message/submitAction`."""
    typed = json.dumps({"reasons": reasons, "text": note} | marker)
    value = {"actionName": "feedback", "actionValue": {"reaction": "dislike", "feedback": typed}}
    reply = await post_activity(service, make_invoke("message/submitAction", value))
    await _routed(service)
    return reply


def _message(response: Any) -> str:
    assert response["task"]["type"] == "message", response
    return response["task"]["value"]


def _form(response: Any) -> dict[str, Any]:
    assert response["task"]["type"] == "continue", response
    return response["task"]["value"]["card"]["content"]


async def _rows(db_factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    """Vote rows through plain SQL: adapter tests do not import the ORM."""
    async with db_factory() as session:
        result = await session.execute(
            text(
                "SELECT vote, message_id, channel_id, platform_user_id, feedback_text,"
                " feedback_reasons FROM message_feedback ORDER BY created_at"
            )
        )
        return list(result.mappings())


async def _escalations(db_factory: async_sessionmaker[AsyncSession]) -> int:
    async with db_factory() as session:
        return (
            await session.execute(text("SELECT count(*) FROM support_escalations"))
        ).scalar_one()


def _posts_to_ops(fake: TeamsApiFake) -> list[str]:
    return [
        str(r.body.get("text")) for r in fake.activity_requests if f"/conversations/{OPS}/" in r.url
    ]


def test_feedback_text_reads_teams_json_and_tolerates_plain_text() -> None:
    assert feedback_text('{"feedbackText": " too slow "}') == "too slow", "Teams' JSON, trimmed"
    assert feedback_text('{"other": 1}') == "", "no text key, no text"
    assert feedback_text("plain") == "plain", "a bare string is the text itself"


def test_a_form_keeps_only_known_reasons_in_vocabulary_order() -> None:
    form = form_details({"message": "m-1", "reasons": "too_slow,bogus, inaccurate", "text": " "})
    assert form.reasons == ("inaccurate", "too_slow"), "unknown codes dropped, order fixed"
    assert form.text == "", "whitespace is no text"


def test_a_sent_form_is_told_apart_from_the_built_in_one() -> None:
    marked = sent_form(json.dumps({"action": FEEDBACK_DIALOG, "message": "forged"}), "m-1")
    assert marked is not None and marked.message_id == "m-1", "the invoke's answer, not the form's"
    inputs = sent_form(json.dumps({"reasons": "other", "text": " x "}), "m-1")
    assert inputs is not None and (inputs.reasons, inputs.text) == (("other",), "x")
    assert sent_form(json.dumps({"feedbackText": "x"}), "m-1") is None, "the built-in form"
    assert sent_form("plain", "m-1") is None, "not JSON, not the form"


def test_a_channel_answer_links_to_its_thread_and_a_chat_answer_names_the_chat() -> None:
    channel = AnswerPlace("19:c@thread.tacv2;messageid=100", "200", is_channel=True)
    link = channel.link(ENTRA_TENANT_ID)
    assert link.startswith("https://teams.microsoft.com/l/message/19%3Ac%40thread.tacv2/200?")
    assert "parentMessageId=100" in link, "the link opens the answer in its thread"
    assert AnswerPlace(CONVERSATION_ID, "m-1", is_channel=False).link("t") == IN_DIRECT_CHAT


async def test_a_thumbs_up_is_recorded_and_thanked(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        reply = await _click(service, "like")

    assert _message(reply) == feedback.THANKS_VOTE, "a light thanks, no form"
    [row] = await _rows(db_session_factory)
    assert (row["vote"], row["message_id"], row["channel_id"]) == ("up", ANSWER, CONVERSATION_ID)
    assert row["platform_user_id"] == AAD_OBJECT_ID, "on the voter's own row"


async def test_a_thumbs_down_is_recorded_and_opens_the_form_with_every_reason(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        form = _form(await _click(service, "dislike"))

    assert_card_renders(form)
    [reasons] = [element for element in form["body"] if element["type"] == "Input.ChoiceSet"]
    assert [choice["value"] for choice in reasons["choices"]] == list(FEEDBACK_REASONS)
    assert reasons["isMultiSelect"] is True, "any number of reasons"
    assert feedback.SHARED_HINT not in json.dumps(form), "nothing is shared unless routed"
    [row] = await _rows(db_session_factory)
    assert row["vote"] == "down", "the click is a vote before the form is sent"


async def test_the_form_stores_its_reasons_and_text_on_the_submitters_row(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        await _click(service, "dislike")
        reply = await _send(service, reasons="inaccurate,too_slow", note=CRITICISM)

    assert _message(reply) == feedback.THANKS_TEXT
    [row] = await _rows(db_session_factory)
    assert (row["vote"], row["feedback_text"]) == ("down", CRITICISM)
    assert row["feedback_reasons"] == ["inaccurate", "too_slow"], "the codes, as Slack stores them"


async def test_an_empty_form_is_shown_again_and_records_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        form = _form(await _send(service, note="   "))

    assert feedback.NEEDS_ONE in json.dumps(form), "the person can fix it rather than lose it"
    assert await _rows(db_session_factory) == [], "no vote from a form refused as empty"


@pytest.mark.parametrize(
    "policy",
    [
        TenantAccessPolicy(invoker_user_ids=(OTHER_AAD_OBJECT_ID,)),
        TenantAccessPolicy(protected_channel_ids=(CONVERSATION_ID,)),
    ],
    ids=["outside-invoker-allowlist", "protected"],
)
async def test_someone_who_could_not_ask_there_cannot_vote(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    policy: TenantAccessPolicy,
) -> None:
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    async with _running(db_session_factory, teams_api_fake) as service:
        up = await _click(service, "like")
        down = await _click(service, "dislike")
        sent = await _send(service, reasons="other", note=CRITICISM)

    assert {_message(up), _message(down), _message(sent)} == {NOT_ALLOWED}, "told, every time"
    assert await _rows(db_session_factory) == [], "a refused vote records nothing"


async def test_teams_built_in_form_on_an_older_answer_still_records(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    typed = json.dumps({"feedbackText": CRITICISM})
    value = {"actionName": "feedback", "actionValue": {"reaction": "dislike", "feedback": typed}}
    async with _running(db_session_factory, teams_api_fake, routed=True) as service:
        await post_activity(service, make_invoke("message/submitAction", value))

    [row] = await _rows(db_session_factory)
    assert (row["vote"], row["feedback_text"], row["feedback_reasons"]) == ("down", CRITICISM, None)
    assert _posts_to_ops(teams_api_fake) == [], "that form never said it was shared"


@pytest.mark.parametrize("case", ["other organisation", "archived"])
async def test_feedback_is_dropped_unless_from_the_live_organisation(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake, case: str
) -> None:
    value = {"actionName": "feedback", "actionValue": {"reaction": "like", "feedback": ""}}
    invoke = make_invoke("message/submitAction", value)
    if case == "archived":
        await set_provision_status(db_session_factory, tenant_id=TENANT, archive=True)
    else:
        invoke["conversation"] = {
            "id": CONVERSATION_ID,
            "conversationType": "personal",
            "tenantId": "00000000-0000-0000-0000-000000000099",
        }
    async with _running(db_session_factory, teams_api_fake) as service:
        await post_activity(service, invoke)
    assert await _rows(db_session_factory) == [], "no vote from outside the live organisation"


async def test_a_routed_form_says_so_and_is_posted_once_per_change_spending_no_credit(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await get_tenant(session, TENANT)
        await make_thread_session(
            session,
            tenant=tenant,
            platform="teams",
            thread_id=CONVERSATION_ID,
            ma_session_id="sesn_fb",
            ma_agent_id="agent_fb",
        )
    with structlog.testing.capture_logs() as logs:
        async with _running(db_session_factory, teams_api_fake, routed=True) as service:
            form = _form(await _click(service, "dislike"))
            await _send(service, reasons="inaccurate", note=CRITICISM)
            await _send(service, reasons="inaccurate", note=CRITICISM)
            await _send(service, reasons="inaccurate,other", note=CRITICISM)

    assert feedback.SHARED_HINT in json.dumps(form), "the form says it is shared before it is"
    first, changed = _posts_to_ops(teams_api_fake)
    assert first.startswith(f"**\N{THUMBS DOWN SIGN} Feedback** from {USER_NAME}")
    assert AAD_OBJECT_ID in first and IN_DIRECT_CHAT in first, "who, and where the answer is"
    assert "Agent `agent_fb`, session `sesn_fb`" in first
    assert "**Reasons:** Wrong or inaccurate" in first and first.endswith(CRITICISM)
    assert "\n" not in first.replace("\n\n", ""), "a paragraph a line: Teams drops single breaks"
    assert "Something else" in changed, "a changed form is posted again; the same one is not"
    assert await _escalations(db_session_factory) == 0, "a routed form spends no support credit"
    assert CRITICISM not in str(logs), "the text never enters a log record"


async def test_forms_stay_in_the_database_when_routing_is_off(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        await _send(service, reasons="other", note=CRITICISM)
    assert _posts_to_ops(teams_api_fake) == [], "off by default: nothing leaves the database"
    [row] = await _rows(db_session_factory)
    assert row["feedback_text"] == CRITICISM


async def test_a_routed_form_from_a_sealed_channel_is_marked(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    policy = TenantAccessPolicy(channel_rules={CHANNEL_ID: ChannelRule(readers="inside")})
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    data = {"action": FEEDBACK_DIALOG, "message": ANSWER, "reasons": "other", "text": ""}
    submit = make_invoke("task/submit", {"data": data})
    submit["conversation"] = {
        "id": THREAD_ID,
        "conversationType": "channel",
        "tenantId": ENTRA_TENANT_ID,
    }
    async with _running(db_session_factory, teams_api_fake, routed=True) as service:
        await post_activity(service, submit)

    [posted] = _posts_to_ops(teams_api_fake)
    assert support.SEALED_LINE in posted, "whoever reads it knows to answer in the channel"


async def test_the_form_sent_as_a_submit_action_is_recorded_and_routed(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake, routed=True) as service:
        await _click(service, "dislike")
        reply = await _send_as_action(service, reasons="inaccurate", note=CRITICISM)

    assert reply is None, "a message/submitAction is answered with an empty body"
    [row] = await _rows(db_session_factory)
    assert (row["vote"], row["feedback_text"]) == ("down", CRITICISM), "the text is kept"
    assert row["feedback_reasons"] == ["inaccurate"], "and the reasons"
    [posted] = _posts_to_ops(teams_api_fake)
    assert "**Reasons:** Wrong or inaccurate" in posted and posted.endswith(CRITICISM)


async def test_an_empty_form_sent_as_a_submit_action_keeps_only_the_vote(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake, routed=True) as service:
        await _click(service, "dislike")
        await _send_as_action(service, note="  ", action=FEEDBACK_DIALOG, message=ANSWER)

    [row] = await _rows(db_session_factory)
    assert (row["vote"], row["feedback_text"], row["feedback_reasons"]) == ("down", None, None)
    assert _posts_to_ops(teams_api_fake) == [], "nothing to route from an empty form"


async def test_a_form_sent_as_a_submit_action_is_refused_like_the_dialog(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    policy = TenantAccessPolicy(invoker_user_ids=(OTHER_AAD_OBJECT_ID,))
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    async with _running(db_session_factory, teams_api_fake, routed=True) as service:
        await _send_as_action(service, reasons="other", note=CRITICISM)

    assert await _rows(db_session_factory) == [], "the same voter rule on either route"
    assert _posts_to_ops(teams_api_fake) == []


async def test_a_routed_form_is_not_posted_to_a_protected_support_channel(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with db_session_factory.begin() as session:
        policy = TenantAccessPolicy(protected_channel_ids=(OPS,))
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    async with _running(db_session_factory, teams_api_fake, routed=True) as service:
        await _send(service, reasons="other", note=CRITICISM)

    assert _posts_to_ops(teams_api_fake) == [], "the bot does not post where it may not"
    [row] = await _rows(db_session_factory)
    assert row["feedback_text"] == CRITICISM, "the form is still recorded"


async def test_the_submit_names_the_answer_teams_replied_to_over_the_forms_copy(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    data = {"action": FEEDBACK_DIALOG, "message": "forged", "reasons": "other", "text": ""}
    replied = make_invoke("task/submit", {"data": data})
    bare = make_invoke("task/submit", {"data": data | {"message": "m-9"}})
    del bare["replyToId"]
    async with _running(db_session_factory, teams_api_fake) as service:
        await post_activity(service, replied)
        await post_activity(service, bare)

    ids = {row["message_id"] for row in await _rows(db_session_factory)}
    assert ids == {ANSWER, "m-9"}, "replyToId when Teams sends it, else the form's id"
