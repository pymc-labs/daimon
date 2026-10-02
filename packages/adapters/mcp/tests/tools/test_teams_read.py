"""Teams channel reads over a fake Bot Framework and Graph: who may read, and what comes back."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import OPEN_READ_POLICY, ChannelReadPolicy
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._read import (
    _teams_get_message_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_list_channels_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_list_threads_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_parse_link_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_read_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_search_messages_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_TEAM = "19:team@thread.tacv2"
_GROUP = "00000000-0000-4000-8000-0000000000aa"
_CHANNEL = "19:chan@thread.tacv2"
_PRIVATE = "19:secret@thread.tacv2"
_ROOT = "1700000000000"
_THREAD = f"{_CHANNEL};messageid={_ROOT}"


def _message(id: str, text: str, *, user: str = "Ada", app: bool = False) -> dict[str, Any]:
    sender = (
        {"application": {"id": "bot", "displayName": "daimon"}}
        if app
        else {"user": {"id": _CALLER, "displayName": user}}
    )
    return {
        "id": id,
        "messageType": "message",
        "createdDateTime": "2026-10-01T00:00:00Z",
        "from": sender,
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
        "attachments": [],
    }


class _Fake:
    def __init__(self, *, members: frozenset[str] = frozenset({_TEAM, _CHANNEL, _PRIVATE})) -> None:
        self.members = members
        self.requests: list[httpx.Request] = []
        self.graph_status = 200
        self.graph_token_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "login.microsoftonline.com":
            if b"graph.microsoft.com" in request.content and self.graph_token_status != 200:
                return httpx.Response(self.graph_token_status)
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        path = url.path
        if path.endswith(f"/v3/teams/{_TEAM}/conversations"):
            return httpx.Response(
                200,
                json={
                    "conversations": [
                        {"id": _TEAM, "type": "standard"},
                        {"id": _CHANNEL, "name": "research", "type": "standard"},
                        {"id": _PRIVATE, "name": "secret", "type": "private"},
                    ]
                },
            )
        if "/members/" in path:
            conversation = path.split("/v3/conversations/")[1].split("/members/")[0]
            if conversation in self.members:
                return httpx.Response(200, json={"id": "29:x", "aadObjectId": _CALLER})
            return httpx.Response(404)
        if url.host == "graph.microsoft.com":
            if self.graph_status != 200:
                return httpx.Response(self.graph_status)
            if path.endswith("/replies"):
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            _message("1700000000002", "second"),
                            _message("1700000000001", "first"),
                        ],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?$skiptoken=older",
                    },
                )
            if path.endswith(f"/messages/{_ROOT}"):
                return httpx.Response(200, json=_message(_ROOT, "root"))
            if path.endswith("/messages"):
                newer = {**_message("1700000000100", "newer post about budgets"), "replies": []}
                older = {
                    **_message(_ROOT, "root"),
                    "replies": [
                        _message("1700000000002", "reply two", app=True),
                        _message("1700000000001", "reply one"),
                    ],
                    "replies@odata.nextLink": "x",
                }
                system = {**_message("1", ""), "messageType": "systemEventMessage"}
                body: dict[str, Any] = {"value": [newer, older, system]}
                if url.params.get("$skiptoken") != "next":  # "next" is the last page
                    body["@odata.nextLink"] = "https://graph.microsoft.com/v1.0/x?$skiptoken=next"
                    return httpx.Response(200, json=body)
                return httpx.Response(200, json={"value": []})
        return httpx.Response(404)

    def graph(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "graph.microsoft.com"]


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


async def _setup(db_session: AsyncSession) -> AuthIdentity:
    tenant = await make_tenant(db_session)
    await record_teams_installation(
        db_session, tenant_id=tenant.id, team_id=_TEAM, group_id=_GROUP, name="Lab"
    )
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=Role.USER,
        platform="teams",
        platform_user_id=_CALLER,
    )


async def test_read_channel_returns_posts_with_replies_oldest_first(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth, fake = await _setup(db_session), _Fake()
    result = await _teams_read_channel_impl(
        _runtime(fake, sessionmaker),
        auth,
        channel_id=_CHANNEL,
        limit=10,
        cursor="tok",
        read_policy=OPEN_READ_POLICY,
    )
    assert (result.channel_name, result.team_name) == ("research", "Lab")
    assert [p.text for p in result.posts] == ["root", "newer post about budgets"], (
        "system events dropped"
    )
    root = result.posts[0]
    assert [r.text for r in root.replies] == ["reply one", "reply two"], "replies oldest first"
    assert root.replies[1].is_bot and root.more_replies and root.thread_id == _THREAD
    assert result.next_cursor == "next", "the cursor is only the skip token"
    [read] = fake.graph()
    assert read.url.params["$expand"] == "replies" and read.url.params["$skiptoken"] == "tok"
    assert f"/teams/{_GROUP}/channels/" in read.url.path, "addressed by the team's Entra group"
    assert result.trust == "untrusted"


async def test_read_thread_returns_the_root_then_replies(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth, fake = await _setup(db_session), _Fake()
    result = await _teams_read_thread_impl(
        _runtime(fake, sessionmaker),
        auth,
        thread_id=_THREAD,
        limit=50,
        cursor=None,
        read_policy=OPEN_READ_POLICY,
    )
    assert [m.text for m in result.messages] == ["root", "first", "second"]
    assert result.next_cursor == "older"
    paged = await _teams_read_thread_impl(
        _runtime(fake, sessionmaker),
        auth,
        thread_id=_THREAD,
        limit=50,
        cursor="older",
        read_policy=OPEN_READ_POLICY,
    )
    assert [m.text for m in paged.messages] == ["first", "second"], "a later page has no root"


async def test_reads_refuse_non_members_chats_and_unknown_channels(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _setup(db_session)
    outsider = _runtime(_Fake(members=frozenset()), sessionmaker)
    with pytest.raises(ToolError, match="not a member"):
        await _teams_read_channel_impl(
            outsider, auth, channel_id=_CHANNEL, limit=5, cursor=None, read_policy=OPEN_READ_POLICY
        )
    runtime = _runtime(_Fake(), sessionmaker)
    with pytest.raises(ToolError, match="1:1 chat"):
        await _teams_read_thread_impl(
            runtime, auth, thread_id="a:chat", limit=5, cursor=None, read_policy=OPEN_READ_POLICY
        )
    with pytest.raises(ToolError, match="not in a team"):
        await _teams_read_channel_impl(
            runtime,
            auth,
            channel_id="19:other@thread.tacv2",
            limit=5,
            cursor=None,
            read_policy=OPEN_READ_POLICY,
        )


async def test_a_private_channel_is_read_only_from_inside_it(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth, fake = await _setup(db_session), _Fake()
    runtime = _runtime(fake, sessionmaker)
    with pytest.raises(ToolError, match="private channel"):
        await _teams_read_channel_impl(
            runtime, auth, channel_id=_PRIVATE, limit=5, cursor=None, read_policy=OPEN_READ_POLICY
        )
    assert fake.graph() == [], "refused before any Graph read"
    inside = ChannelReadPolicy(policy=OPEN_ACCESS_POLICY, origin_channel_ids=frozenset({_PRIVATE}))
    await _teams_read_channel_impl(
        runtime, auth, channel_id=_PRIVATE, limit=5, cursor=None, read_policy=inside
    )


async def test_a_sealed_channel_is_refused_from_outside(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _setup(db_session)
    sealed = ChannelReadPolicy(policy=TenantAccessPolicy(sealed_channel_ids=frozenset({_CHANNEL})))
    with pytest.raises(ToolError):
        await _teams_get_message_impl(
            _runtime(_Fake(), sessionmaker),
            auth,
            channel_id=_THREAD,
            message_id="1700000000001",
            read_policy=sealed,
        )


async def test_a_sealed_thread_stays_out_of_its_channel_reads(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _setup(db_session)
    runtime = _runtime(_Fake(), sessionmaker)
    sealed = ChannelReadPolicy(policy=TenantAccessPolicy(sealed_channel_ids=frozenset({_THREAD})))
    read = await _teams_read_channel_impl(
        runtime, auth, channel_id=_CHANNEL, limit=5, cursor=None, read_policy=sealed
    )
    assert [p.text for p in read.posts] == ["newer post about budgets"]
    threads = await _teams_list_threads_impl(runtime, auth, channel_id=_CHANNEL, read_policy=sealed)
    assert [t.thread_id for t in threads] == [f"{_CHANNEL};messageid=1700000000100"]
    with pytest.raises(ToolError):
        await _teams_get_message_impl(
            runtime, auth, channel_id=_CHANNEL, message_id=_ROOT, read_policy=sealed
        )


async def test_list_channels_hides_private_channels_from_non_members(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _setup(db_session)
    rows = await _teams_list_channels_impl(
        _runtime(_Fake(members=frozenset({_TEAM, _CHANNEL})), sessionmaker), auth
    )
    assert [(r.name, r.type) for r in rows] == [("General", "standard"), ("research", "standard")]
    assert (
        await _teams_list_channels_impl(_runtime(_Fake(members=frozenset()), sessionmaker), auth)
        == []
    )


async def test_list_threads_and_search_scan_recent_posts(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _setup(db_session)
    runtime = _runtime(_Fake(), sessionmaker)
    threads = await _teams_list_threads_impl(
        runtime, auth, channel_id=_CHANNEL, read_policy=OPEN_READ_POLICY
    )
    assert [(t.preview, t.reply_count) for t in threads] == [
        ("newer post about budgets", 0),
        ("root", 2),
    ]
    found = await _teams_search_messages_impl(
        runtime,
        auth,
        content="BUDGETS",
        channel_ids=[_CHANNEL],
        author_ids=None,
        limit=5,
        read_policy=OPEN_READ_POLICY,
    )
    assert [m.message.text for m in found.matches] == ["newer post about budgets"]
    assert not found.complete and found.hint, "a bounded scan says it stopped short"
    unscoped = await _teams_search_messages_impl(
        runtime,
        auth,
        content="reply",
        channel_ids=None,
        author_ids=[_CALLER.upper()],
        limit=5,
        read_policy=OPEN_READ_POLICY,
    )
    assert {m.channel_name for m in unscoped.matches} == {"General", "research"}, "private skipped"
    assert all(not m.message.is_bot for m in unscoped.matches), "author filter by Entra id"


async def test_graph_refusal_explains_the_missing_consent(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth, fake = await _setup(db_session), _Fake()
    fake.graph_status = 403
    with pytest.raises(ToolError, match="team owner"):
        await _teams_read_channel_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            limit=5,
            cursor=None,
            read_policy=OPEN_READ_POLICY,
        )


async def test_a_refused_graph_token_is_a_tool_error(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth, fake = await _setup(db_session), _Fake()
    fake.graph_token_status = 401
    with pytest.raises(ToolError, match="Graph"):
        await _teams_read_channel_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            limit=5,
            cursor=None,
            read_policy=OPEN_READ_POLICY,
        )


def test_parse_link_reads_message_and_channel_links() -> None:
    message = _teams_parse_link_impl(
        "https://teams.microsoft.com/l/message/19%3Achan%40thread.tacv2/1700000000002"
        f"?tenantId={_ENTRA}&groupId={_GROUP}&parentMessageId={_ROOT}&teamName=Lab"
    )
    assert (message.channel_id, message.message_id, message.thread_id) == (
        _CHANNEL,
        "1700000000002",
        _THREAD,
    )
    root = _teams_parse_link_impl(f"https://teams.cloud.microsoft/l/message/{_CHANNEL}/{_ROOT}")
    assert root.thread_id == _THREAD, "a root post is its own thread"
    channel = _teams_parse_link_impl(
        "https://teams.microsoft.com/l/channel/19%3Achan%40thread.tacv2/research?groupId=g"
    )
    assert (channel.link_type, channel.channel_id) == ("channel", _CHANNEL)
    for bad in (
        "https://evil.example/l/message/19:a/1",
        "http://teams.microsoft.com/l/channel/19:a/x",
    ):
        with pytest.raises(ToolError):
            _teams_parse_link_impl(bad)
