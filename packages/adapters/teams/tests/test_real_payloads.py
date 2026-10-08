"""Contract tests over Bot Framework activities shaped as Teams sends them.

Each `data/activities/<case>.json` keeps the shape of a published Microsoft example or wire
capture (`SOURCES`), with this package's test identifiers. Activities go through the real
`/api/messages` route; only the outbound Bot Framework transport, MSAL, MA and the turn are faked.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic.types.beta import FileMetadata
from daimon.adapters.teams.attachments import InboundFile
from daimon.adapters.teams.identity import (
    DENIED,
    GROUP_CHAT_UNSUPPORTED,
    Refusal,
    TeamsInbound,
    parse_inbound,
)
from daimon.adapters.teams.output_delivery import FILE_INFO_CONTENT_TYPE
from daimon.core import output_delivery
from daimon.core._models import MessageFeedback
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.testing import build_fake_anthropic, list_response, ma_agent
from daimon.testing.ma import MARouter
from microsoft_teams.api import MessageActivity
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_inbound,
    patched_turns,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")

DATA = Path(__file__).parent / "data/activities"
_LEARN = "https://learn.microsoft.com/en-us/microsoftteams/platform"
_EVENTS = f"{_LEARN}/bots/how-to/conversations/subscribe-to-conversation-events"
_FILES = f"{_LEARN}/bots/how-to/bots-filesv4"
_TEAMS_NET = (
    "https://github.com/microsoft/teams.net/blob/085ae6c9dc/test/Microsoft.Teams.Apps.UnitTests"
)
# A 2025 channel thread reply and a 2026 personal `adaptiveCard/action` invoke,
# captured from Teams. Every invoke fixture keeps that invoke's envelope.
_CAPTURE = f"{_TEAMS_NET}/TeamsActivityTests.cs"
_OLD_DOCS = "https://github.com/MicrosoftDocs/msteams-docs/blob"
_TASK_VALUE = (
    f"{_OLD_DOCS}/7969193c6f/msteams-platform/task-modules-and-cards/task-modules/"
    "task-modules-bots.md#payload-of-taskfetch-and-tasksubmit-messages"
)
_CONSENT_VALUE = f"{_FILES}#invoke-activity-when-the-user-accepts-the-file"
SOURCES: dict[str, tuple[str, ...]] = {
    # Plus `conversation.tenantId`, which every newer personal-scope example carries.
    "personal_message": (
        f"{_LEARN}/bots/build-conversational-capability#receive-a-message-activity",
    ),
    # The captured reply, as the root post it hangs off.
    "channel_mention": (
        _CAPTURE,
        f"{_LEARN}/bots/how-to/conversations/channel-and-group-conversations",
    ),
    "channel_thread_reply": (_CAPTURE,),
    # The captured reply with files uploaded to it: the activity carries only the text, and
    # the files are `reference` attachments on Graph's copy of the message.
    "channel_attachments_reply": (
        _CAPTURE,
        "https://learn.microsoft.com/en-us/graph/api/resources/chatmessageattachment",
    ),
    # The captured reply with a pasted image and a file marked in its HTML.
    "channel_media_reply": (
        _CAPTURE,
        "https://learn.microsoft.com/en-us/graph/api/resources/chatmessageattachment",
        "https://learn.microsoft.com/en-us/graph/api/chatmessagehostedcontent-get",
    ),
    # The captured reply quoting the bot instead of mentioning it: the SDK's quotedReply
    # entity (senderId the quoted author's Bot Framework id) and its text placeholder.
    "channel_quote_reply": (
        _CAPTURE,
        "https://github.com/microsoft/teams.py/blob/main/packages/api/src/microsoft_teams/api/"
        "models/entity/quoted_reply_entity.py",
        "https://microsoft.github.io/teams-sdk/blog/quoted-and-threaded-replies/",
    ),
    # The captured message with the documented group chat conversation.
    "group_chat_message": (
        _CAPTURE,
        f"{_LEARN}/messaging-extensions/how-to/action-commands/create-task-module",
    ),
    "personal_file": (
        f"{_TEAMS_NET}/Files/FilesAccessorWireShapeTests.cs",
        f"{_FILES}#receive-files-in-personal-chat",
    ),
    "pasted_image": (
        "https://github.com/microsoft/teams-ai/blob/e6ba513d68/teams.md/src/pages/templates/"
        "in-depth-guides/file-handling/receiving-inline-images.mdx",
        "https://github.com/microsoft/BotBuilder-Samples/blob/0b664eae3c/archive/samples/"
        "csharp_dotnetcore/56.teams-file-upload/Bots/TeamsFileUploadBot.cs",
    ),
    "adaptive_card_action": (
        _CAPTURE,
        f"{_LEARN}/task-modules-and-cards/cards/Universal-actions-for-adaptive-cards/"
        "Sequential-Workflows#invoke-request-received-on-bot-side",
    ),
    "task_fetch": (_CAPTURE, _TASK_VALUE),
    "task_submit": (_CAPTURE, _TASK_VALUE),
    # The docs' placeholder upload URL is a SharePoint upload session here.
    "file_consent_accept": (_CAPTURE, _CONSENT_VALUE),
    "file_consent_decline": (_CAPTURE, _CONSENT_VALUE),
    "feedback": (
        _CAPTURE,
        f"{_OLD_DOCS}/6c9a179db1/msteams-platform/bots/how-to/"
        "bot-messages-ai-generated-content.md#handle-feedback",
    ),
    # A 👎 on an answer with the custom feedback loop: the SDK's model of the invoke.
    "feedback_fetch": (
        _CAPTURE,
        "https://github.com/microsoft/teams.py/blob/main/packages/api/src/microsoft_teams/api/"
        "activities/invoke/message/fetch_task.py",
    ),
    # The custom loop's form sent: Teams delivers it as `message/submitAction`, the dialog's
    # inputs JSON-encoded in `feedback`, which Microsoft's sample decodes; here daimon's inputs.
    "feedback_form_submit": (
        _CAPTURE,
        "https://github.com/OfficeDev/Microsoft-Teams-Samples/blob/c845867cf0/samples/TeamsSDK/"
        "bot-ai-messages/python/bot-ai-messages/main.py",
    ),
    "installation_add": (f"{_EVENTS}#install-update-event",),
    # No published `remove` payload: a captured personal `upgrade`, with the documented action.
    "installation_remove": (f"{_TEAMS_NET}/ActivitiesTests.cs", f"{_EVENTS}#install-update-event"),
    "members_added": (f"{_EVENTS}#members-added",),
    "members_removed": (f"{_EVENTS}#members-removed",),
}

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SERVICE_PATH = f"/amer/{ENTRA_TENANT_ID}"  # The fixtures' serviceUrl, minus its trailing slash.
GROUP_CHAT_ID = "19:group-chat-1@thread.v2"
CARD_MESSAGE_ID = "1700000000100"  # Every invoke's replyToId.
IMAGE_URL = "https://smba.trafficmanager.net/amer/v3/attachments/0-eus-d1-synthetic/views/original"
DOWNLOAD_URL = (
    "https://example.sharepoint.com/personal/synthetic_user_example_com/_layouts/15/download.aspx"
    "?UniqueId=00000000-0000-4000-8000-00000000f11e&Translate=false"
    "&tempauth=synthetic-not-a-real-token&ApiVersion=2.1"
)
# case: (kind, conversation, text without the bot's mention, files)
MESSAGES: dict[str, tuple[str, str, str, tuple[InboundFile, ...]]] = {
    "personal_message": (
        "dm",
        CONVERSATION_ID,
        "Hello Teams TestAgent.Sending bold-italic rich text",
        (),
    ),
    "channel_mention": ("channel", THREAD_ID, "summarise this week's releases", ()),
    "channel_thread_reply": ("channel", THREAD_ID, "reply to thread", ()),
    "channel_attachments_reply": ("channel", THREAD_ID, "describe these attachments", ()),
    "channel_media_reply": (
        "channel",
        THREAD_ID,
        "describe these attachments",
        (InboundFile("embedded_image", "image"), InboundFile("embedded_file", "file")),
    ),
    "channel_quote_reply": (
        "channel",
        THREAD_ID,
        '[quoting daimon: "Releases ship on Thursdays."]\ndoes this still hold?',
        (),
    ),
    "personal_file": (
        "dm",
        CONVERSATION_ID,
        "",
        (InboundFile("shared_file", "quarterly_report.pdf", DOWNLOAD_URL),),
    ),
    "pasted_image": ("dm", CONVERSATION_ID, "", (InboundFile("pasted_image", "image", IMAGE_URL),)),
}


def _load(case: str) -> dict[str, Any]:
    return json.loads((DATA / f"{case}.json").read_text())


def _parse(payload: dict[str, Any]) -> TeamsInbound | Refusal:
    activity = MessageActivity.model_validate(payload)
    return parse_inbound(
        activity, configured_tenant=ENTRA_TENANT_ID, service_url=activity.service_url
    )


def _posts(fake: TeamsApiFake) -> list[dict[str, object]]:
    return [r.body for r in fake.activity_requests if r.method == "POST"]


async def _run(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    case: str,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """POST `case` to the real service and drain it; the invoke response and each turn's kwargs."""
    runtime = build_teams_runtime(db_factory, http_client=http_client)
    with patched_turns() as turns:
        async with running_service(runtime, fake) as service:
            response = await post_activity(service, _load(case))
            await service.turns.drain(timeout=30)
    return response, turns


def test_every_fixture_names_its_source() -> None:
    fixtures = sorted(path.stem for path in DATA.glob("*.json"))
    assert fixtures == sorted(SOURCES), "each payload records where its shape comes from"


@pytest.mark.parametrize("case", MESSAGES)
def test_real_messages_parse_to_verified_facts(case: str) -> None:
    kind, conversation, text, files = MESSAGES[case]
    inbound = _parse(_load(case))
    assert isinstance(inbound, TeamsInbound), f"{case} passes the identity checks"
    assert (inbound.kind, inbound.user_id, inbound.conversation_id) == (
        kind,
        AAD_OBJECT_ID,
        conversation,
    )
    assert inbound.channel_id == conversation.split(";", 1)[0]
    assert inbound.text == text, "the bot's mention is stripped"
    assert inbound.files == files, "the text/html sibling is not a file"


def test_group_chat_message_is_refused() -> None:
    assert _parse(_load("group_chat_message")) == Refusal(GROUP_CHAT_UNSUPPORTED)


def test_personal_message_without_a_conversation_tenant_is_denied() -> None:
    """The documented 1:1 example predates `conversation.tenantId`; identity fails closed."""
    payload = _load("personal_message")
    del payload["conversation"]["tenantId"]
    assert _parse(payload) == Refusal(DENIED)


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize(
    "case", ["personal_message", "channel_mention", "channel_thread_reply", "channel_quote_reply"]
)
async def test_real_message_runs_a_turn_answered_in_its_conversation(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake, case: str
) -> None:
    _, conversation, text, _ = MESSAGES[case]
    _, [turn] = await _run(db_session_factory, teams_api_fake, case)
    assert f">{text}</user_query>" in turn["user_message"]
    card = next(r for r in teams_api_fake.activity_requests if r.method == "POST")
    assert (
        httpx.URL(card.url).path == f"{SERVICE_PATH}/v3/conversations/{conversation}/activities"
    ), "the status card goes to the activity's own service URL, trailing slash and all"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_group_chat_message_is_answered_with_the_refusal(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    _, turns = await _run(db_session_factory, teams_api_fake, "group_chat_message")
    assert turns == [], "no turn runs in a group chat"
    [reply] = teams_api_fake.activity_requests
    assert str(reply.body.get("text")).endswith(GROUP_CHAT_UNSUPPORTED), "a quoted reply"
    assert (
        httpx.URL(reply.url).path == f"{SERVICE_PATH}/v3/conversations/{GROUP_CHAT_ID}/activities"
    )


@pytest.mark.usefixtures("provisioned_tenant")
async def test_pasted_image_reaches_the_turn_as_a_vision_block(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    fetched: list[httpx.Request] = []

    def media(request: httpx.Request) -> httpx.Response:
        fetched.append(request)
        png = io.BytesIO()
        Image.new("L", (4, 4)).save(png, "PNG")
        return httpx.Response(200, content=png.getvalue())

    http = httpx.AsyncClient(transport=httpx.MockTransport(media))
    _, [turn] = await _run(db_session_factory, teams_api_fake, "pasted_image", http)
    assert len(turn["image_blocks"]) == 1, "the pasted image is inlined"
    assert [str(r.url) for r in fetched] == [IMAGE_URL], "fetched from the service URL's host"
    assert fetched[0].headers["authorization"].startswith("Bearer "), "with the bot's token"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_shared_file_reaches_the_turn_as_a_download_line(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    _, [turn] = await _run(db_session_factory, teams_api_fake, "personal_file")
    assert "[attachment] `quarterly_report.pdf`" in turn["user_message"]
    assert DOWNLOAD_URL in turn["user_message"], "the agent gets the pre-authorised URL"
    assert turn["image_blocks"] is None, "a PDF is linked, not inlined"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_card_action_is_answered_with_the_documented_card_response(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    response, _ = await _run(db_session_factory, teams_api_fake, "adaptive_card_action")
    assert (response["statusCode"], response["type"]) == (
        200,
        "application/vnd.microsoft.card.adaptive",
    ), "Universal Actions expects a card to replace the clicked one"
    assert response["value"]["type"] == "AdaptiveCard"
    assert "Routines" in json.dumps(response["value"]), "the refreshed routines panel"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_task_fetch_opens_the_dialog_and_task_submit_creates_the_routine(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    agent = ma_agent(tenant_id=TENANT, name="daimon").model_dump(mode="json")
    router = MARouter()
    router.add("GET", r"/v1/agents$", lambda r, m: list_response([agent]))
    runtime = build_teams_runtime(
        db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        teams=teams_settings(admins=(AAD_OBJECT_ID,)),
    )
    async with running_service(runtime, teams_api_fake) as service:
        opened = await post_activity(service, _load("task_fetch"))
        submitted = await post_activity(service, _load("task_submit"))

    assert opened["task"]["type"] == "continue", "the create form opens"
    assert (
        opened["task"]["value"]["card"]["contentType"] == "application/vnd.microsoft.card.adaptive"
    )
    assert submitted["task"]["type"] == "message"
    assert "Created routine on daimon" in submitted["task"]["value"]
    edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert edits and edits[-1].url.endswith(f"/activities/{CARD_MESSAGE_ID}"), (
        "the panel the dialog came from refreshes in place"
    )


async def _consent(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> tuple[object, list[httpx.Request], list[str]]:
    """Offer one file, then click the card with `case`; the invoke's answer, uploads, deletes."""
    monkeypatch.setattr(output_delivery, "_POLL_DELAYS_S", (0.0,))  # One listing settles.
    uploads: list[httpx.Request] = []
    deletes: list[str] = []
    listing = FileMetadata(
        id="file_1",
        created_at="2026-09-01T00:00:00Z",
        filename="file_example.txt",
        mime_type="text/plain",
        size_bytes=4,
        type="file",
        downloadable=True,
    )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    def on_upload(request: httpx.Request) -> httpx.Response:
        uploads.append(request)
        return httpx.Response(201, json={"id": "item-1"})

    router = MARouter()
    router.add("GET", r"/v1/files$", lambda r, m: list_response([listing.model_dump(mode="json")]))
    router.add(
        "GET", r"/v1/files/([^/]+)/content", lambda r, m: httpx.Response(200, content=b"text")
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    runtime = build_teams_runtime(
        db_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(on_upload)),
    )
    payload = _load(case)
    async with running_service(runtime, fake) as service:
        await service.turns.outputs.sweep(make_inbound(), "sesn_1")
        [offer] = _posts(fake)
        card: Any = offer["attachments"]
        context = card[0]["content"][f"{payload['value']['action']}Context"]
        payload["value"]["context"] = context  # Teams echoes it back.
        response = await post_activity(service, payload)
        await service.turns.drain(timeout=30)
    return response, uploads, deletes


@pytest.mark.usefixtures("provisioned_tenant")
async def test_file_consent_accept_uploads_to_the_session_teams_offers(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, uploads, deletes = await _consent(
        db_session_factory, teams_api_fake, monkeypatch, "file_consent_accept"
    )

    upload_info = _load("file_consent_accept")["value"]["uploadInfo"]
    assert response is None, "the invoke is acknowledged with an empty 200"
    assert [(r.method, str(r.url)) for r in uploads] == [("PUT", upload_info["uploadUrl"])]
    assert deletes == ["file_1"], "the output is deleted once uploaded"
    shown: Any = _posts(teams_api_fake)[-1]["attachments"]
    assert shown[0]["contentType"] == FILE_INFO_CONTENT_TYPE
    assert shown[0]["content"]["uniqueId"] == upload_info["uniqueId"]


@pytest.mark.usefixtures("provisioned_tenant")
async def test_file_consent_decline_deletes_without_uploading(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, uploads, deletes = await _consent(
        db_session_factory, teams_api_fake, monkeypatch, "file_consent_decline"
    )

    assert response is None
    assert uploads == [] and deletes == ["file_1"]
    assert not any("file_example.txt" in str(p.get("text")) for p in _posts(teams_api_fake)), (
        "a decline posts nothing"
    )


@pytest.mark.usefixtures("provisioned_tenant")
async def test_feedback_is_recorded_and_acknowledged_with_an_empty_body(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    response, _ = await _run(db_session_factory, teams_api_fake, "feedback")
    assert response is None, "Teams answers a message/submitAction reply with a body with 400"
    async with db_session_factory() as session:
        [row] = (await session.execute(select(MessageFeedback))).scalars().all()
    assert (row.vote, row.feedback_text) == ("up", "This is my feedback.")
    assert (row.message_id, row.channel_id) == (CARD_MESSAGE_ID, CONVERSATION_ID)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_custom_feedback_click_records_the_vote_and_answers_with_the_form(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    response, _ = await _run(db_session_factory, teams_api_fake, "feedback_fetch")
    assert response["task"]["type"] == "continue", "a dialog, as `message/fetchTask` expects"
    assert response["task"]["value"]["title"] == "What went wrong?"
    async with db_session_factory() as session:
        [row] = (await session.execute(select(MessageFeedback))).scalars().all()
    assert (row.vote, row.message_id) == ("down", CARD_MESSAGE_ID), "the 👎 is recorded at once"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_the_custom_form_sent_as_submit_action_stores_its_reasons_and_text(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    response, _ = await _run(db_session_factory, teams_api_fake, "feedback_form_submit")
    assert response is None, "acknowledged with an empty body, as every message/submitAction"
    async with db_session_factory() as session:
        [row] = (await session.execute(select(MessageFeedback))).scalars().all()
    assert (row.vote, row.message_id) == ("down", CARD_MESSAGE_ID), "on the answer replied to"
    assert row.feedback_text == "The totals are off.", "the form's text, not dropped"
    assert row.feedback_reasons == ["inaccurate", "too_slow"], "the form's reasons, as codes"


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("case", ["installation_remove", "members_added", "members_removed"])
async def test_lifecycle_events_are_acknowledged_and_ignored(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake, case: str
) -> None:
    response, turns = await _run(db_session_factory, teams_api_fake, case)
    assert response is None, "an empty 200"
    assert turns == [] and teams_api_fake.activity_requests == [], "nothing is posted"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_an_install_is_acknowledged_and_welcomed_in_the_selected_channel(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    response, turns = await _run(db_session_factory, teams_api_fake, "installation_add")
    assert response is None and turns == []
    [welcome] = teams_api_fake.activity_requests
    assert "/v3/conversations/19:channel-1@thread.tacv2/activities" in welcome.url
