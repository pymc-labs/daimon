"""replace_channel_file on Teams: a new version of a channel file, or a refusal that says why."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._send import (
    TeamsReplacedFile,
    _teams_replace_file_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_TEAM = "19:team@thread.tacv2"
_GROUP = "00000000-0000-4000-8000-0000000000aa"
_CHANNEL = "19:chan@thread.tacv2"
_OTHER_CHANNEL = "19:other@thread.tacv2"
# A well-formed origin id no turn has: fixed, so test ids match across xdist workers.
_UNKNOWN_ORIGIN = "00000000-0000-4000-8000-0000000000ff"
_THREAD = f"{_CHANNEL};messageid=1700000000000"
_LINK = "https://example.sharepoint.com/sites/Lab/Shared%20Documents/research/deck.pptx"
_WEB_URL = "https://example.sharepoint.com/sites/Lab/_layouts/15/Doc.aspx?file=deck.pptx"
_VERSION = '"{DECK},1"'


class _Fake:
    """Bot Framework and Graph for one team whose `research` channel holds `deck.pptx`.

    `folder` is the deck's parent folder: `f1` is the channel's, `f2` a sibling's;
    `share_drive` the drive `/shares` names, `d1` the channel's; `lookup_status`
    answers `/shares`; `put` answers the write, a status or a whole response;
    `deck` is the item's own fields. `token_fails_after_read` refuses a token once
    the deck was read, so the write fails before it goes out.
    """

    def __init__(
        self,
        *,
        folder: str = "f1",
        share_drive: str = "d1",
        lookup_status: int = 200,
        put: int | httpx.Response | type[httpx.TransportError] = 200,
        deck: dict[str, object] | None = None,
        member: bool = True,
        token_fails_after_read: bool = False,
    ) -> None:
        self.folder, self.share_drive, self.lookup_status = folder, share_drive, lookup_status
        self.put, self.deck, self.member = put, deck or {}, member
        self.token_fails_after_read = token_fails_after_read
        self.read_deck = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, path = request.url, request.url.path
        if url.host == "login.microsoftonline.com":
            if self.token_fails_after_read and self.read_deck:
                return httpx.Response(400, json={"error": "invalid_client"})
            # A token that is never reused, so every Graph call asks for one.
            expires = 0 if self.token_fails_after_read else 3600
            return httpx.Response(200, json={"access_token": "tok", "expires_in": expires})
        if path.endswith(f"/v3/teams/{_TEAM}/conversations"):
            channels = [{"id": _TEAM}, {"id": _CHANNEL, "name": "research", "type": "standard"}]
            return httpx.Response(200, json={"conversations": channels})
        if "/members/" in path:
            if not self.member:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": "29:x", "aadObjectId": _CALLER})
        if url.host != "graph.microsoft.com":
            return httpx.Response(404)
        if path.endswith("/filesFolder"):
            return httpx.Response(200, json={"id": "f1", "parentReference": {"driveId": "d1"}})
        if path.startswith("/v1.0/shares/u!"):
            if self.lookup_status != 200:
                return httpx.Response(self.lookup_status, json={"error": {"code": "denied"}})
            found = {"id": "deck", "parentReference": {"driveId": self.share_drive}}
            return httpx.Response(200, json=found)
        if path == "/v1.0/drives/d1/items/deck" and request.method == "GET":
            self.read_deck = True
            parent = {"driveId": "d1", "id": self.folder}
            item = {"id": "deck", "name": "deck.pptx", "eTag": _VERSION, "parentReference": parent}
            return httpx.Response(200, json=item | self.deck)
        if path == "/v1.0/drives/d1/items/f2":
            return httpx.Response(200, json={"id": "f2", "parentReference": {"driveId": "d1"}})
        if path == "/v1.0/drives/d1/items/deck/content" and request.method == "PUT":
            if isinstance(self.put, httpx.Response):
                return self.put
            if isinstance(self.put, type):
                raise self.put("connection dropped", request=request)
            if self.put != 200:
                return httpx.Response(self.put, json={"error": {"code": "refused"}})
            saved = {"id": "deck", "name": "deck.pptx", "eTag": '"{DECK},2"', "webUrl": _WEB_URL}
            return httpx.Response(200, json=saved)
        return httpx.Response(404)

    def writes(self) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method in ("PUT", "POST", "PATCH") and r.url.host == "graph.microsoft.com"
        ]

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


async def _setup(db_session: AsyncSession, filename: str) -> tuple[AuthIdentity, str, str]:
    """The caller, a handle holding the edited `filename`, and its turn's origin id.

    The turn runs in a thread of `_CHANNEL`.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await record_teams_installation(
        db_session, tenant_id=tenant.id, team_id=_TEAM, group_id=_GROUP, name="Lab"
    )
    now = dt.datetime.now(dt.UTC)
    row, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="deck",
        display_filename=filename,
        content_type="application/octet-stream",
        now=now,
    )
    await store_upload_content(db_session, upload_token=token, data=b"edited deck", now=now)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="teams",
        parent_channel_id=_CHANNEL,
        thread_id=_THREAD,
        responder_ma_agent_id="agent_x",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        expires_at=now + dt.timedelta(minutes=10),
        now=now,
    )
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="teams",
        platform_user_id=_CALLER,
        chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_x"),
    )
    return auth, row.id, str(origin.id)


async def _replace(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    fake: _Fake,
    *,
    channel_id: str = _CHANNEL,
    version: str = _VERSION,
    filename: str = "deck.pptx",
    origin: str | None = "",
) -> TeamsReplacedFile:
    """Replace the deck from a turn in `_CHANNEL`; `origin` names another origin id, or None."""
    auth, handle, origin_id = await _setup(db_session, filename)
    return await _teams_replace_file_impl(
        _runtime(fake, sessionmaker),
        auth,
        channel_id=channel_id,
        link=_LINK,
        version=version,
        file_handle=handle,
        origin_context_id=origin_id if origin == "" else origin,
    )


@pytest.mark.parametrize("version", [_VERSION, "{DECK},1", ' "{DECK},1" '])
async def test_a_channel_file_gets_a_new_version_at_the_version_read(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], version: str
) -> None:
    """One conditional PUT over the item, whether or not the agent kept the eTag's quotes."""
    fake = _Fake()
    replaced = await _replace(db_session, sessionmaker, fake, channel_id=_THREAD, version=version)
    [put] = fake.writes()
    assert put.url.path == "/v1.0/drives/d1/items/deck/content", "the item itself, not a copy"
    assert put.headers["If-Match"] == _VERSION, "the eTag just read, as Graph wrote it"
    assert put.content == b"edited deck", "the staged bytes"
    assert replaced.version == "{DECK},2", "the new version, bare, for a further replace"
    assert replaced.link == _LINK, "the link that resolved, to pass again"
    assert replaced.markdown == f"- [deck.pptx]({_WEB_URL})", "a reply line linking the file"
    assert fake.posts() == [], "the agent's reply carries the link; the tool posts nothing"


async def test_a_file_changed_since_it_was_read_is_refused_before_any_write(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    fake = _Fake()
    with pytest.raises(ToolError, match="changed since you read it"):
        await _replace(db_session, sessionmaker, fake, version='"{DECK},0"')
    assert fake.writes() == [], "an older version is never written over a newer one"


@pytest.mark.parametrize(
    ("put", "match"),
    [
        (423, "locked, most likely open in a desktop Office app"),
        (412, "changed since you read it"),
        (403, r"HTTP 403\), so nothing was changed: daimon's app needs the Files.ReadWrite.All"),
        (401, r"refused daimon the write to deck.pptx \(HTTP 401\)"),
        (400, r"did not save deck.pptx \(HTTP 400\); nothing was changed"),
        (500, r"did not finish \(HTTP 500\); it may or may not have been saved"),
        (httpx.ReadTimeout, r"did not finish \(ReadTimeout\); it may or may not have been saved"),
    ],
)
async def test_a_failed_write_says_whether_the_file_may_have_changed(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    put: int | type[httpx.TransportError],
    match: str,
) -> None:
    """A refusal changed nothing; a 5xx or a lost answer may have saved, so say so."""
    fake = _Fake(put=put)
    with pytest.raises(ToolError, match=match):
        await _replace(db_session, sessionmaker, fake)
    assert len(fake.writes()) == 1, f"{put}: one write, never retried"


async def test_a_saved_file_whose_answer_does_not_parse_is_reported_saved(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Graph answered 200, so the version was saved; only its new eTag is unknown."""
    fake = _Fake(put=httpx.Response(200, content=b"<html>"))
    replaced = await _replace(db_session, sessionmaker, fake)
    assert replaced.version is None, "the new version is unknown, never the old one"
    assert replaced.link == _LINK, "the link still resolves the file"
    assert len(fake.writes()) == 1, "one write"


async def test_a_write_that_fails_before_sending_says_nothing_changed(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """No token for the write: no request went out, so nothing may have been saved."""
    fake = _Fake(token_fails_after_read=True)
    with pytest.raises(ToolError, match="failed before anything was sent .* nothing was changed"):
        await _replace(db_session, sessionmaker, fake)
    assert fake.writes() == [], "the write never left"


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_lookup_names_the_missing_permission(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], status: int
) -> None:
    fake = _Fake(lookup_status=status)
    with pytest.raises(ToolError, match=r"Files.ReadWrite.All.*Nothing was changed"):
        await _replace(db_session, sessionmaker, fake)
    assert fake.writes() == [], "nothing written without the permission"


@pytest.mark.parametrize(
    ("fake", "where"),
    [
        (_Fake(folder="f2"), "another folder of the same library"),
        (_Fake(share_drive="d2"), "another channel's site or library"),
    ],
)
async def test_a_file_outside_the_channels_folder_is_refused(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    fake: _Fake,
    where: str,
) -> None:
    """A file elsewhere is not this channel's to change, though the permission reaches it."""
    with pytest.raises(ToolError, match="not a file in this channel's Files folder"):
        await _replace(db_session, sessionmaker, fake)
    assert fake.writes() == [], f"nothing written in {where}"


async def test_a_folder_passed_as_the_link_is_refused(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A folder named like the file is still no file to save over."""
    fake = _Fake(deck={"name": "deck.pptx", "folder": {"childCount": 2}})
    with pytest.raises(ToolError, match="is a folder"):
        await _replace(db_session, sessionmaker, fake)
    assert fake.writes() == [], "a folder is never written"


async def test_a_new_version_of_another_file_type_is_refused(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    fake = _Fake()
    with pytest.raises(ToolError, match="same type of file"):
        await _replace(db_session, sessionmaker, fake, filename="deck.pdf")
    assert fake.writes() == [], "a .pptx never gets PDF bytes"


@pytest.mark.parametrize(
    ("channel_id", "origin", "match"),
    [
        (_OTHER_CHANNEL, "", "only a file of the channel this conversation is in"),
        (_CHANNEL, None, "needs this turn's origin_context_id"),
        (_CHANNEL, _UNKNOWN_ORIGIN, "needs this turn's origin_context_id"),
    ],
)
async def test_only_the_channel_the_turn_started_in_is_written(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    channel_id: str,
    origin: str | None,
    match: str,
) -> None:
    """A turn in one channel never saves over another channel's file, even one it may post to."""
    fake = _Fake()
    with pytest.raises(ToolError, match=match):
        await _replace(db_session, sessionmaker, fake, channel_id=channel_id, origin=origin)
    assert fake.requests == [], "refused before any Teams or Graph call"


@pytest.mark.parametrize(
    ("channel_id", "member", "match"),
    [
        (_CHANNEL, False, "not a member"),
        ("a:chat-1", True, "only a file in a Teams channel"),
    ],
)
async def test_a_caller_who_cannot_post_there_or_a_chat_is_refused(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    channel_id: str,
    member: bool,
    match: str,
) -> None:
    """The same roster check as a post: the bot never writes for someone outside the channel."""
    fake = _Fake(member=member)
    with pytest.raises(ToolError, match=match):
        await _replace(db_session, sessionmaker, fake, channel_id=channel_id)
    graph = [r for r in fake.requests if r.url.host == "graph.microsoft.com"]
    assert graph == [], "refused before any Graph call"
