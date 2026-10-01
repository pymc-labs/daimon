"""Channel turns over Microsoft Graph: replayed history, pasted images, shared files.

Activities go through the real `/api/messages` route; Graph is a `MockTransport` on the
runtime's HTTP client, Bot Framework the `TeamsApiFake`, and the turn is patched.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.adapters.teams.app import _NO_CONTEXT
from daimon.adapters.teams.identity import TeamsInbound, parse_inbound
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.thread_sessions import get_latest_thread_session
from microsoft_teams.api import MessageActivity
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ENTRA_TENANT_ID,
    TEAM_GROUP_ID,
    TeamsApiFake,
    build_teams_runtime,
    patched_turns,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")

DATA = Path(__file__).parent / "data/activities"
ROOT, REPLY = "1700000000001", "1700000000003"
CHANNEL_PATH = f"/v1.0/teams/{TEAM_GROUP_ID}/channels/19:channel-1@thread.tacv2/messages"
HOSTED = (
    f"https://graph.microsoft.com{CHANNEL_PATH}/{ROOT}/replies/{REPLY}"
    "/hostedContents/aWQ9eF8wLXd1cy1kMTAtc3ludGhldGlj/$value"
)


def _load(case: str) -> dict[str, Any]:
    return json.loads((DATA / f"{case}.json").read_text())


def _bare_mention() -> dict[str, Any]:
    payload = copy.deepcopy(_load("channel_thread_reply"))
    payload["text"] = "<at>daimon</at>"
    payload["attachments"] = []
    return payload


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (4, 4)).save(buffer, "PNG")
    return buffer.getvalue()


def _graph_message(id: str, html: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": id,
        "replyToId": None if id == ROOT else ROOT,
        "messageType": "message",
        "createdDateTime": "2026-01-01T00:00:00Z",
        "deletedDateTime": None,
        "from": {
            "application": None,
            "user": {"id": "u-2", "displayName": "Grace Hopper", "userIdentityType": "aadUser"},
        },
        "body": {"contentType": "html", "content": html},
        "attachments": [],
        "mentions": [],
    } | extra


MEDIA_MESSAGE = _graph_message(
    REPLY,
    '<p><at id="0">daimon</at>&nbsp;describe these attachments</p>'
    f'<p><img src="{HOSTED}" width="250" height="150" alt="image"></p>'
    '<attachment id="6d4bb0c3-0000-4000-8000-00000000f11e"></attachment>',
    attachments=[
        {
            "id": "6d4bb0c3-0000-4000-8000-00000000f11e",
            "contentType": "reference",
            "contentUrl": "https://example.sharepoint.com/sites/team/Shared%20Documents/q3.xlsx",
            "content": None,
            "name": "q3.xlsx",
            "thumbnailUrl": None,
        }
    ],
)


SITE_ID = "example.sharepoint.com,2c1f0a9e-0000-4000-8000-000000000001,7d3b-web"
DOWNLOAD_URL = (
    "https://example.sharepoint.com/sites/team/_layouts/15/download.aspx?UniqueId=q3&tempauth=x"
)
# What Graph answers once an admin granted the app the team's site (`Sites.Selected`).
GRANTED_SITE = {
    f"/v1.0/teams/{TEAM_GROUP_ID}/channels/19:channel-1@thread.tacv2/filesFolder": {
        "id": "01FOLDER",
        "name": "General",
        "parentReference": {"driveId": "b!drive-1"},
    },
    "/v1.0/sites/example.sharepoint.com:/sites/team": {"id": SITE_ID},
    f"/v1.0/groups/{TEAM_GROUP_ID}/sites/root": {"id": SITE_ID},
    f"/v1.0/sites/{SITE_ID}/drives": {
        "value": [
            {
                "id": "b!drive-1",
                "webUrl": "https://example.sharepoint.com/sites/team/Shared%20Documents",
            }
        ]
    },
    "/v1.0/drives/b!drive-1/root:/q3.xlsx": {
        "id": "01Q3",
        "name": "q3.xlsx",
        "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
    },
}


def _graph(
    seen: list[httpx.Request],
    *,
    site: dict[str, Any] | None = None,
    newer: tuple[dict[str, Any], ...] = (),
) -> httpx.AsyncClient:
    """Graph answering the fixtures' thread: a root post, two replies, the media message.

    `site` adds SharePoint answers by path; without it the team's files are refused.
    `newer` are replies posted after the media message.
    """
    replies = {
        "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#Collection(chatMessage)",
        "value": [
            *newer,
            MEDIA_MESSAGE,
            _graph_message("1700000000002", "<p>the numbers are in the sheet</p>"),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == f"{CHANNEL_PATH}/{ROOT}":
            return httpx.Response(200, json=_graph_message(ROOT, "<p>Q3 release plan</p>"))
        if path == f"{CHANNEL_PATH}/{ROOT}/replies":
            return httpx.Response(200, json=replies)
        if path == f"{CHANNEL_PATH}/{ROOT}/replies/{REPLY}":
            return httpx.Response(200, json=MEDIA_MESSAGE)
        if path == httpx.URL(HOSTED).path:
            return httpx.Response(200, content=_png(), headers={"Content-Type": "image/png"})
        if site and path in site:
            return httpx.Response(200, json=site[path])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _run(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    payload: dict[str, Any],
    http_client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    runtime = build_teams_runtime(db_factory, http_client=http_client)
    with patched_turns() as turns:
        async with running_service(runtime, fake) as service:
            await post_activity(service, payload)
            await service.turns.drain(timeout=30)
    return turns


def _texts(fake: TeamsApiFake) -> str:
    return json.dumps([r.body for r in fake.activity_requests], ensure_ascii=False)


def test_a_channel_message_keeps_its_team_for_the_graph_lookup() -> None:
    activity = MessageActivity.model_validate(_load("channel_media_reply"))
    inbound = parse_inbound(
        activity, configured_tenant=ENTRA_TENANT_ID, service_url=activity.service_url
    )
    assert isinstance(inbound, TeamsInbound)
    assert inbound.team_id == "19:team@thread.tacv2"
    assert inbound.team_group_id is None, "channel messages omit aadGroupId; it is looked up"


async def test_a_channel_reply_replays_the_thread_inlines_the_image_and_explains_the_file(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    seen: list[httpx.Request] = []
    [turn] = await _run(
        db_session_factory, teams_api_fake, _load("channel_media_reply"), _graph(seen)
    )

    message = turn["user_message"]
    assert 'files="unavailable"/>' in message, "the agent is told channel files do not work"
    assert '<thread_history source="teams" trust="untrusted">' in message
    assert "Q3 release plan" in message and "the numbers are in the sheet" in message
    assert "describe these attachments</message>" not in message, "the trigger is not history"
    assert len(turn["image_blocks"] or []) == 1, "the hosted image becomes a vision block"
    assert "[attachment] `q3.xlsx` was shared but can't be opened here." in message
    assert "I couldn't read `q3.xlsx` (files shared in channels need a 1:1 chat)." in _texts(
        teams_api_fake
    ), "the person hears the file was not read"
    assert {r.url.host for r in seen} == {"graph.microsoft.com"}, "Graph only"
    assert all(r.headers["Authorization"] == "Bearer test-bot-token" for r in seen)
    lookups = [r for r in teams_api_fake.requests if "/v3/teams/" in r.url]
    assert lookups, "the group id came from Bot Framework's team details, absent from the activity"


async def test_a_channel_reply_whose_activity_names_no_media_is_read_from_graph(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """As captured: Teams sends the bot only the text; the files exist on Graph's copy."""
    seen: list[httpx.Request] = []
    [turn] = await _run(
        db_session_factory, teams_api_fake, _load("channel_attachments_reply"), _graph(seen)
    )

    message = turn["user_message"]
    assert len(turn["image_blocks"] or []) == 1, "the hosted image is found on Graph"
    assert "[attachment] `q3.xlsx` was shared but can't be opened here." in message
    assert "I couldn't read `q3.xlsx`" in _texts(teams_api_fake), "the person hears it"
    assert any(r.url.path == f"{CHANNEL_PATH}/{ROOT}/replies/{REPLY}" for r in seen), (
        "the mentioned message is read from Graph even with no markers in the activity"
    )


async def test_a_channel_file_on_a_granted_site_is_linked_and_the_agent_told_files_work(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    seen: list[httpx.Request] = []
    graph = _graph(seen, site=GRANTED_SITE)
    [turn] = await _run(db_session_factory, teams_api_fake, _load("channel_media_reply"), graph)

    message = turn["user_message"]
    assert 'files="available"/>' in message, "the agent learns this channel takes files"
    assert "`q3.xlsx`, shared by the user with this message" in message
    assert DOWNLOAD_URL in message, "the pre-authorised SharePoint link, for the sandbox to fetch"
    assert "q3.xlsx" not in _texts(teams_api_fake), "nothing to apologise for"
    assert {r.url.host for r in seen} == {"graph.microsoft.com"}, "resolved through Graph only"


async def test_the_watermark_is_the_newest_message_read_not_the_bots_answer(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Replies posted while a turn runs are older than its answer: the next delta must keep them."""
    newer = _graph_message("1700000000004", "<p>also check the budget</p>")
    payload = _load("channel_media_reply")
    await _run(db_session_factory, teams_api_fake, payload, _graph([], newer=(newer,)))

    async with db_session_factory() as session:
        row = await get_latest_thread_session(
            session,
            tenant_id=derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID),
            platform="teams",
            thread_id=payload["conversation"]["id"],
        )
    assert row is not None and row.watermark_message_id == "1700000000004", (
        "the newest reply the turn read, not the answer posted after it"
    )


async def test_without_graph_the_turn_runs_and_the_person_hears_what_was_missed(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    [turn] = await _run(db_session_factory, teams_api_fake, _load("channel_media_reply"))

    assert "thread_history" not in turn["user_message"], "no history when Graph refuses"
    assert not turn["image_blocks"]
    assert (
        "I couldn't read a pasted image (I can't read this channel's messages), "
        "a shared file (files shared in channels need a 1:1 chat)."
    ) in _texts(teams_api_fake), "never a silent drop"


async def test_a_bare_mention_runs_over_the_replayed_thread(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    seen: list[httpx.Request] = []
    [turn] = await _run(db_session_factory, teams_api_fake, _bare_mention(), _graph(seen))
    assert "Q3 release plan" in turn["user_message"], "the thread is what it asks about"
    assert turn["user_message"].endswith('is_admin="false"></user_query>')


async def test_a_bare_mention_without_history_says_so_instead_of_running(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    turns = await _run(db_session_factory, teams_api_fake, _bare_mention())
    assert turns == [], "nothing to answer from"
    assert _NO_CONTEXT in _texts(teams_api_fake), "the card says why"
