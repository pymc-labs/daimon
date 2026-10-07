"""Channel turns over Microsoft Graph: replayed history, pasted images, shared files.

Activities go through the real `/api/messages` route; Graph is a `MockTransport` on the
runtime's HTTP client, Bot Framework the `TeamsApiFake`, and the turn is patched.
"""

from __future__ import annotations

import copy
import io
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.adapters.teams.app import _NO_CONTEXT
from daimon.adapters.teams.identity import TeamsInbound, parse_inbound
from daimon.adapters.teams.site_grant import GrantTarget, verify_state
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.teams_sharepoint import ENABLE_FILES_TOOL
from daimon.core.turn.state import ToolUseBlock
from daimon.testing import (
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_agent_env_echo_handler,
)
from daimon.testing.ma import NotHandled
from microsoft_teams.api import MessageActivity
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    ENTRA_TENANT_ID,
    TEAM_GROUP_ID,
    TeamsApiFake,
    build_teams_runtime,
    patched_turns,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")

DATA = Path(__file__).parent / "data/activities"
ROOT, REPLY = "1700000000001", "1700000000003"
CHANNEL = "19:channel-1@thread.tacv2"
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
        if path.startswith(("/v1.0/sites/", "/v1.0/groups/")) or path.endswith("/filesFolder"):
            # As captured without Sites.Selected: Graph denies the team's SharePoint.
            return httpx.Response(403, json={"error": {"code": "accessDenied"}})
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
    """As captured, a channel activity names its team's group; one without it is looked up."""
    for name, group in (
        ("channel_attachments_reply", TEAM_GROUP_ID),
        ("channel_media_reply", None),
    ):
        activity = MessageActivity.model_validate(_load(name))
        inbound = parse_inbound(
            activity, configured_tenant=ENTRA_TENANT_ID, service_url=activity.service_url
        )
        assert isinstance(inbound, TeamsInbound)
        assert inbound.team_id == "19:team@thread.tacv2"
        assert inbound.team_group_id == group


async def test_a_channel_reply_replays_the_thread_inlines_the_image_and_explains_the_file(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    seen: list[httpx.Request] = []
    [turn] = await _run(
        db_session_factory, teams_api_fake, _load("channel_media_reply"), _graph(seen)
    )

    message = turn["user_message"]
    assert 'files="unavailable" files_hint=' in message, "the agent is told files do not work"
    assert "until the deployment's operator grants" in message, (
        "with no public URL there is no card to offer, so the hint names the operator"
    )
    assert '<thread_history source="teams" trust="untrusted">' in message
    assert "Q3 release plan" in message and "the numbers are in the sheet" in message
    assert "describe these attachments</message>" not in message, "the trigger is not history"
    assert len(turn["image_blocks"] or []) == 1, "the hosted image becomes a vision block"
    assert (
        "[attachment] `q3.xlsx` was shared but can't be opened: daimon has no access" in message
    ), "the agent can tell the person why"
    assert "q3.xlsx" not in _texts(teams_api_fake), "nothing is posted besides the answer"
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
    assert "[attachment] `q3.xlsx` was shared but can't be opened: daimon has no access" in message
    assert "q3.xlsx" not in _texts(teams_api_fake), "nothing is posted besides the answer"
    assert any(r.url.path == f"{CHANNEL_PATH}/{ROOT}/replies/{REPLY}" for r in seen), (
        "the mentioned message is read from Graph even with no markers in the activity"
    )


@pytest.mark.parametrize("admin", [True, False])
async def test_an_admin_whose_channel_files_are_refused_gets_the_enable_files_sign_in(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    admin: bool,
) -> None:
    """A card, once: the sign-in that grants the team's site goes to a daimon admin only."""
    admins = (AAD_OBJECT_ID,) if admin else ()
    teams = teams_settings(admins=admins, public_url="https://teams.example")
    runtime = build_teams_runtime(db_session_factory, teams=teams, http_client=_graph([]))
    with patched_turns():
        async with running_service(runtime, teams_api_fake) as service:
            for _ in range(2):
                await post_activity(service, _load("channel_attachments_reply"))
                await service.turns.drain(timeout=30)

    offers = [text for text in _cards(teams_api_fake) if "Enable files" in text]
    if not admin:
        assert offers == [], "only a daimon admin is offered the sign-in"
        return
    [offer] = offers
    assert "login.microsoftonline.com" in offer and "Sites.FullControl.All" in offer
    assert "redirect_uri=https%3A%2F%2Fteams.example%2Foauth%2Fteams%2Ffiles%2Fcallback" in offer
    assert _signed_target(offer) == GrantTarget(TEAM_GROUP_ID, CHANNEL), "for this channel"


async def test_the_agents_enable_files_call_posts_the_card_for_this_channel(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Asked for files, the agent calls `enable_channel_files` (here through the search proxy).

    The card follows the answer though nothing was refused: files already work here.
    """
    teams = teams_settings(admins=(AAD_OBJECT_ID,), public_url="https://teams.example")
    anthropic = build_fake_anthropic(combine_handlers(_no_outputs, make_agent_env_echo_handler()))
    graph = _graph([], site=GRANTED_SITE)
    runtime = build_teams_runtime(
        db_session_factory, anthropic=anthropic, teams=teams, http_client=graph
    )
    asked = ToolUseBlock(
        kind="tool_use",
        id="t1",
        type="agent.mcp_tool_use",
        name="call_tool",
        input={"name": ENABLE_FILES_TOOL, "arguments": {}},
        mcp_server_name="daimon-mcp",
        status="complete",
    )
    with patched_turns(tools=(asked,)) as turns:
        async with running_service(runtime, teams_api_fake) as service:
            await post_activity(service, _load("channel_attachments_reply"))
            await service.turns.drain(timeout=30)

    [offer] = [text for text in _cards(teams_api_fake) if "Enable files" in text]
    assert _signed_target(offer) == GrantTarget(TEAM_GROUP_ID, CHANNEL), "for this channel"
    assert "files_hint" not in turns[0]["user_message"], "files work here: no hint"


async def test_where_files_are_refused_the_hint_tells_the_agent_to_call_for_the_card(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """With a public URL the card can be posted, so the hint names the tool, before any format."""
    teams = teams_settings(public_url="https://teams.example")
    runtime = build_teams_runtime(db_session_factory, teams=teams, http_client=_graph([]))
    with patched_turns() as turns:
        async with running_service(runtime, teams_api_fake) as service:
            await post_activity(service, _load("channel_attachments_reply"))
            await service.turns.drain(timeout=30)

    message = turns[0]["user_message"]
    assert f"call {ENABLE_FILES_TOOL} first" in message, "the agent knows to post the card"
    assert "Do not swap in an artifact, report or notebook" in message, "not a fallback first"


def _no_outputs(request: httpx.Request) -> httpx.Response:
    """A tool call sends the output sweep looking for session files: there are none."""
    if request.method == "GET" and request.url.path == "/v1/files":
        return list_response([])
    raise NotHandled


def _signed_target(card: str) -> GrantTarget | None:
    url = re.search(r'"(https://login\.microsoftonline\.com/[^"]+)"', card)
    assert url is not None, "the card links the sign-in"
    [state] = parse_qs(urlparse(url.group(1)).query)["state"]
    return verify_state(state, secret="test-secret", now=time.time())


def _cards(fake: TeamsApiFake) -> list[str]:
    return [json.dumps(r.body) for r in fake.activity_requests if r.body.get("attachments")]


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


async def test_without_graph_the_turn_runs_and_the_agent_hears_what_was_missed(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    [turn] = await _run(db_session_factory, teams_api_fake, _load("channel_media_reply"))

    message = turn["user_message"]
    assert "thread_history" not in message, "no history when Graph refuses"
    assert not turn["image_blocks"]
    unread = "was shared but can't be opened: daimon could not read this channel message."
    assert f"an image {unread}" in message and f"a file {unread}" in message, "never silent"
    assert "couldn't read" not in _texts(teams_api_fake), "the answer explains it, not a notice"


async def test_without_graph_the_agent_knows_a_channel_messages_media_went_unread(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """The captured activity names no media, so only the agent is told, not the person."""
    [turn] = await _run(db_session_factory, teams_api_fake, _load("channel_attachments_reply"))

    assert "images and files could not be read" in turn["user_message"]
    assert "couldn't read" not in _texts(teams_api_fake), "no notice on every text-only message"


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
