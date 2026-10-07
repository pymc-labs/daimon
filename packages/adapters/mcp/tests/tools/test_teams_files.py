"""send_message with file_handles on Teams: a channel's Files, a 1:1 consent card, refusals."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._send import (
    _teams_send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.core.stores.teams_channel_sites import upsert_teams_channel_site
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.core.teams_file_offers import UploadOffer, verify_offer
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_TEAM = "19:team@thread.tacv2"
_GROUP = "00000000-0000-4000-8000-0000000000aa"
_CHANNEL = "19:chan@thread.tacv2"
_THREAD = f"{_CHANNEL};messageid=1700000000000"
_PRIVATE = "19:secret@thread.tacv2"
_SHARED = "19:shared@thread.tacv2"
_CHAT = "a:chat-1"
_WEB_URL = "https://contoso.sharepoint.com/sites/Lab/Shared%20Documents/research/chart.png"


class _Fake:
    def __init__(self, *, graph_status: int = 200, post_status: int = 200) -> None:
        self.graph_status, self.post_status = graph_status, post_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, path = request.url, request.url.path
        if url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path.endswith(f"/v3/teams/{_TEAM}/conversations"):
            channels = [
                {"id": _TEAM},
                {"id": _CHANNEL, "name": "research", "type": "standard"},
                {"id": _PRIVATE, "name": "secret", "type": "private"},
                {"id": _SHARED, "name": "client", "type": "shared"},
            ]
            return httpx.Response(200, json={"conversations": channels})
        if "/members/" in path:
            return httpx.Response(200, json={"id": "29:x", "aadObjectId": _CALLER})
        if url.host == "graph.microsoft.com":
            if self.graph_status != 200:
                return httpx.Response(self.graph_status)
            if path.endswith("/filesFolder"):
                return httpx.Response(200, json={"id": "f1", "parentReference": {"driveId": "d1"}})
            if request.method == "PUT":
                return httpx.Response(
                    201, json={"id": "i1", "name": "chart.png", "webUrl": _WEB_URL}
                )
        if path.endswith("/activities"):
            if self.post_status != 200:
                return httpx.Response(self.post_status)
            return httpx.Response(200, json={"id": f"act-{len(self.posts())}"})
        return httpx.Response(404)

    def graph(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "graph.microsoft.com"]

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/activities")]


def _runtime(fake: _Fake, sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    client = TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        client_id="app-id",
        client_secret="secret",
        tenant_id=_ENTRA,
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        settings=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        deployment_default=DeploymentDefault(),
        teams_client=client,
    )


async def _setup(db_session: AsyncSession) -> tuple[AuthIdentity, str]:
    """The caller, and a handle holding `chart.png`."""
    tenant = await make_tenant(db_session)
    await record_teams_installation(
        db_session, tenant_id=tenant.id, team_id=_TEAM, group_id=_GROUP, name="Lab"
    )
    now = dt.datetime.now(dt.UTC)
    row, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="chart",
        display_filename="chart.png",
        content_type="image/png",
        now=now,
    )
    await store_upload_content(db_session, upload_token=token, data=b"png-bytes", now=now)
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=Role.USER,
        platform="teams",
        platform_user_id=_CALLER,
    )
    return auth, row.id


async def test_a_channel_file_is_saved_to_its_files_and_linked(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake()
    row = await _teams_send_message_impl(
        _runtime(fake, sessionmaker),
        auth,
        channel_id=_THREAD,
        content="the chart",
        file_handles=[handle],
    )
    assert row.conversation_id == _THREAD
    [put] = [r for r in fake.requests if r.method == "PUT"]
    assert put.url.path.endswith("/drives/d1/items/f1:/chart.png:/content")
    assert put.content == b"png-bytes"
    [post] = fake.posts()
    assert json.loads(post.content)["text"] == f"the chart\n\n- [chart.png]({_WEB_URL})"


async def test_a_channel_without_site_access_refuses_and_posts_nothing(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake(graph_status=403)
    with pytest.raises(ToolError, match="SharePoint site is not granted"):
        await _teams_send_message_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            content="x",
            file_handles=[handle],
        )
    assert fake.posts() == []


async def test_a_private_channel_takes_no_files_until_they_are_turned_on(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake()
    with pytest.raises(ToolError, match="enable_channel_files"):
        await _teams_send_message_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_PRIVATE,
            content="x",
            file_handles=[handle],
        )
    assert fake.graph() == [] and fake.posts() == [], "never the team site's same-named folder"


async def test_a_private_channel_turned_on_saves_to_its_stored_folder(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The folder an admin's sign-in found is used as is: the app cannot look it up."""
    (auth, handle), fake = await _setup(db_session), _Fake()
    await upsert_teams_channel_site(
        db_session,
        tenant_id=auth.tenant_id,
        channel_id=_PRIVATE,
        group_id=_GROUP,
        site_id="contoso.sharepoint.com,s,w",
        drive_id="d-private",
        folder_id="f-private",
    )
    await _teams_send_message_impl(
        _runtime(fake, sessionmaker),
        auth,
        channel_id=_PRIVATE,
        content="the chart",
        file_handles=[handle],
    )
    assert [(r.method, r.url.path) for r in fake.graph()] == [
        ("PUT", "/v1.0/drives/d-private/items/f-private:/chart.png:/content")
    ], "straight to the stored folder, no filesFolder or team-site lookup"
    assert len(fake.posts()) == 1, "the message links the saved file"


async def test_a_failed_post_after_saving_says_the_files_are_saved(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake(post_status=500)
    with pytest.raises(ToolError, match="were saved") as refused:
        await _teams_send_message_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            content="x",
            file_handles=[handle],
        )
    assert _WEB_URL in str(refused.value), "the links let the agent post again without uploading"


async def test_a_chat_file_is_a_consent_card_with_a_signed_offer(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake()
    row = await _teams_send_message_impl(
        _runtime(fake, sessionmaker), auth, channel_id=_CHAT, content="", file_handles=[handle]
    )
    [post] = fake.posts()
    assert row.activity_id == "act-1", "an empty content sends only the card"
    [card] = json.loads(post.content)["attachments"]
    assert card["contentType"] == "application/vnd.microsoft.teams.card.file.consent"
    assert card["content"]["sizeInBytes"] == len(b"png-bytes")
    token = card["content"]["acceptContext"]["upload"]
    assert verify_offer(token, secret="secret", now=time.time()) == UploadOffer(
        handle, _CALLER, _CHAT
    )
    assert verify_offer(token, secret="other", now=time.time()) is None
    assert verify_offer(token, secret="secret", now=time.time() + 3601) is None


@pytest.mark.parametrize(
    ("channel_id", "handles", "match"),
    [
        ("19:group@thread.v2", None, "not a group chat"),
        (_CHAT, ["missing"], "not found"),
    ],
)
async def test_files_refused_where_teams_takes_none_or_the_handle_is_unknown(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    channel_id: str,
    handles: list[str] | None,
    match: str,
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake()
    with pytest.raises(ToolError, match=match):
        await _teams_send_message_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=channel_id,
            content="x",
            file_handles=handles or [handle],
        )
    assert fake.posts() == []


async def test_an_external_participant_in_a_shared_channel_gets_the_tool_error(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake()
    with pytest.raises(ToolError, match="SharePoint site is not granted"):
        await _teams_send_message_impl(
            _runtime(fake, sessionmaker),
            dataclasses.replace(auth, is_external=True),
            channel_id=_SHARED,
            content="x",
            file_handles=[handle],
        )
    assert fake.graph() == [] and fake.posts() == [], "refused before anything is saved"
