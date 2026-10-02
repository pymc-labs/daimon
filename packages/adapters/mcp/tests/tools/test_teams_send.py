"""Teams Bot Framework client and the send_message/create_thread impls, over a fake transport."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._send import (
    _teams_create_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
    TeamsSettings,
)
from daimon.core.mcp_auth import mint_jwt
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.teams_threads import new_setup_thread_id
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_CHANNEL = "19:abc@thread.tacv2"
_THREAD = f"{_CHANNEL};messageid=1700000000000"
_TOKEN_URL = f"https://login.microsoftonline.com/{_ENTRA}/oauth2/v2.0/token"
_BASE = "https://smba.trafficmanager.net/teams/v3/conversations"


class _Fake:
    """Routes Bot Framework calls; records every request."""

    def __init__(self, *, member: str | None = _CALLER, roster_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.member = member
        self.roster_status = roster_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == _TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/members/" in url:
            if self.member is None:
                return httpx.Response(404)
            return httpx.Response(self.roster_status, json={"aadObjectId": self.member})
        if request.method == "POST" and url == _BASE:
            return httpx.Response(200, json={"id": f"{_CHANNEL};messageid=9", "activityId": "9"})
        return httpx.Response(200, json={"id": "act-1"})

    def posts(self) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.method in ("POST", "PUT") and "login." not in str(r.url)
        ]


def _client(fake: _Fake, clock: list[float] | None = None) -> TeamsBotClient:
    now = clock if clock is not None else [0.0]
    return TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        client_id="app-id",
        client_secret="secret",
        tenant_id=_ENTRA,
        clock=lambda: now[0],
    )


def _runtime(
    client: TeamsBotClient | None, sessionmaker: async_sessionmaker[AsyncSession] | None = None
) -> McpRuntime:
    """`sessionmaker` is read for the access policy once membership is confirmed."""
    return McpRuntime(
        session_factory=sessionmaker or MagicMock(),  # type: ignore[arg-type]  # read by the policy
        client=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        settings=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        deployment_default=DeploymentDefault(),
        teams_client=client,
    )


def _auth(
    platform_user_id: str | None = _CALLER, tenant_id: uuid.UUID | None = None
) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        role=Role.USER,
        platform="teams",
        external_id=_ENTRA,
        platform_user_id=platform_user_id,
    )


async def test_token_is_cached_then_refreshed_before_expiry() -> None:
    fake, clock = _Fake(), [0.0]
    client = _client(fake, clock)
    await client.send(_THREAD, "one")
    await client.send(_THREAD, "two")
    clock[0] = 3600 - 299  # inside the refresh margin
    await client.send(_THREAD, "three")
    token_calls = [r for r in fake.requests if str(r.url) == _TOKEN_URL]
    assert len(token_calls) == 2, "one token per validity window, refreshed before expiry"
    assert b"scope=https%3A%2F%2Fapi.botframework.com%2F.default" in token_calls[0].content


async def test_send_posts_markdown_with_ai_label() -> None:
    fake = _Fake()
    assert await _client(fake).send(_THREAD, "**hi**") == "act-1"
    (post,) = fake.posts()
    assert str(post.url) == f"{_BASE}/{_THREAD}/activities"
    assert post.headers["Authorization"] == "Bearer tok"
    body = json.loads(post.content)
    assert (body["text"], body["textFormat"]) == ("**hi**", "markdown")
    assert body["entities"][0]["additionalType"] == ["AIGeneratedContent"]


async def test_a_throttled_send_is_retried_once_after_retry_after() -> None:
    fake = _Fake()
    throttled: list[int] = []

    def _throttle_first_post(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/activities" in str(request.url) and not throttled:
            throttled.append(1)
            return httpx.Response(429, headers={"Retry-After": "0"})
        return fake(request)

    client = TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(_throttle_first_post)),
        client_id="app-id",
        client_secret="secret",
        tenant_id=_ENTRA,
    )
    assert await client.send("a:chat", "hi") == "act-1"
    assert len(fake.posts()) == 1 and throttled == [1], "sent once more after the 429"


async def test_update_card_puts_to_the_activity() -> None:
    fake = _Fake()
    await _client(fake).update_card(_THREAD, "act-1", {"type": "AdaptiveCard"})
    (put,) = fake.posts()
    assert (put.method, str(put.url)) == ("PUT", f"{_BASE}/{_THREAD}/activities/act-1"), (
        "an edit replaces the posted activity in place"
    )
    assert json.loads(put.content)["id"] == "act-1", "the replacement names the activity it edits"


async def test_create_thread_posts_a_channel_conversation() -> None:
    fake = _Fake()
    assert await _client(fake).create_thread(_CHANNEL, "new") == (f"{_CHANNEL};messageid=9", "9")
    body = json.loads(fake.posts()[0].content)
    assert body["channelData"] == {"channel": {"id": _CHANNEL}}
    assert (body["isGroup"], body["tenantId"]) == (True, _ENTRA)


async def test_is_member_matches_case_insensitively_and_404_is_no() -> None:
    assert await _client(_Fake(member=_CALLER.upper())).is_member(_CHANNEL, _CALLER)
    assert not await _client(_Fake(member=None)).is_member(_CHANNEL, _CALLER)
    with pytest.raises(httpx.HTTPStatusError):
        await _client(_Fake(roster_status=500)).is_member(_CHANNEL, _CALLER)


async def test_send_message_checks_the_channel_roster_then_posts(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fake = _Fake()
    row = await _teams_send_message_impl(
        _runtime(_client(fake), sessionmaker), _auth(), channel_id=_THREAD, content="hello"
    )
    assert (row.conversation_id, row.activity_id) == (_THREAD, "act-1")
    roster = next(r for r in fake.requests if "/members/" in str(r.url))
    assert str(roster.url) == f"{_BASE}/{_CHANNEL}/members/{_CALLER}", "thread → channel roster"


async def test_send_message_to_a_setup_conversation_posts_to_its_chat(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fake = _Fake()
    chat = "a:chat-1"
    row = await _teams_send_message_impl(
        _runtime(_client(fake), sessionmaker),
        _auth(),
        channel_id=new_setup_thread_id(chat),
        content="hi",
    )
    assert row.conversation_id == chat
    assert all(f"/{chat}/" in str(r.url) for r in fake.requests if "/v3/" in str(r.url))


@pytest.mark.parametrize(
    ("fake", "match"),
    [
        (_Fake(member=None), "not a member"),
        (_Fake(member="someone-else"), "not a member"),
        (_Fake(roster_status=500), "could not confirm"),
    ],
)
async def test_send_message_refuses_unless_membership_is_confirmed(fake: _Fake, match: str) -> None:
    with pytest.raises(ToolError, match=match):
        await _teams_send_message_impl(
            _runtime(_client(fake)), _auth(), channel_id=_THREAD, content="hi"
        )
    assert fake.posts() == [], "nothing may be posted without a confirmed membership"


async def test_send_message_fails_closed_on_transport_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        client_id="a",
        client_secret="s",
        tenant_id=_ENTRA,
    )
    with pytest.raises(ToolError, match="could not confirm"):
        await _teams_send_message_impl(_runtime(client), _auth(), channel_id=_THREAD, content="hi")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"channel_id": _THREAD, "content": "x", "file_handles": ["h"]}, "/mnt/session/outputs"),
        ({"channel_id": _THREAD, "content": "x", "attachments": [{"a": "b"}]}, "text only"),
        ({"channel_id": _THREAD, "content": "x" * 6_001}, "6,000-character"),
        ({"channel_id": _THREAD, "content": "  "}, "must not be empty"),
        ({"channel_id": "19:a/../b", "content": "x"}, "Teams conversation id"),
    ],
)
async def test_send_message_rejects_bad_input_before_any_call(
    kwargs: dict[str, object], match: str
) -> None:
    fake = _Fake()
    with pytest.raises(ToolError, match=match):
        await _teams_send_message_impl(_runtime(_client(fake)), _auth(), **kwargs)  # type: ignore[arg-type]
    assert fake.requests == []


async def test_send_message_needs_a_client_and_a_teams_user() -> None:
    with pytest.raises(ToolError, match="not configured"):
        await _teams_send_message_impl(_runtime(None), _auth(), channel_id=_THREAD, content="x")
    with pytest.raises(ToolError, match="teams-bound identity"):
        await _teams_send_message_impl(
            _runtime(_client(_Fake())), _auth(None), channel_id=_THREAD, content="x"
        )


async def test_create_thread_posts_to_the_channel_and_refuses_a_thread_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fake = _Fake()
    row = await _teams_create_thread_impl(
        _runtime(_client(fake), sessionmaker), _auth(), channel_id=_CHANNEL, content="kickoff"
    )
    assert row.conversation_id == f"{_CHANNEL};messageid=9"
    with pytest.raises(ToolError, match="not into a thread"):
        await _teams_create_thread_impl(
            _runtime(_client(fake)), _auth(), channel_id=_THREAD, content="x"
        )


@pytest.mark.parametrize(
    ("protected", "tool", "target"),
    [
        (_CHANNEL, _teams_send_message_impl, _THREAD),
        (_THREAD, _teams_send_message_impl, _THREAD),
        (_CHANNEL, _teams_create_thread_impl, _CHANNEL),
    ],
    ids=["thread-under-protected-channel", "protected-thread", "create-thread"],
)
async def test_a_protected_channel_refuses_the_post_and_nothing_is_sent(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    protected: str,
    tool: Callable[..., Awaitable[object]],
    target: str,
) -> None:
    """SYS-048: protection covers a channel and every thread under it."""
    tenant = await make_tenant(db_session, platform="teams", workspace_id=_ENTRA)
    policy = TenantAccessPolicy(protected_channel_ids=(protected,))
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    fake = _Fake()
    with pytest.raises(ToolError, match="protected"):
        await tool(
            _runtime(_client(fake), sessionmaker),
            _auth(tenant_id=tenant.id),
            channel_id=target,
            content="hi",
        )
    assert fake.posts() == [], "a protected channel must receive no post"


async def test_teams_caller_reaches_only_teams_tagged_channel_tools(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="teams", workspace_id=_ENTRA)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    secret = "a" * 32
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(secret), public_url=HttpUrl("https://x/mcp")),
            teams=TeamsSettings(client_id="app-id", client_secret=SecretStr("s"), tenant_id=_ENTRA),
            _env_file=None,  # type: ignore[call-arg]  # isolate from the repo .env
        ),
        sessionmaker=sessionmaker,
    )
    token = mint_jwt(account_id=account.id, secret=secret.encode(), now=dt.datetime.now(dt.UTC))

    async def call(name: str, arguments: dict[str, object]) -> str:
        result = (await call_mcp_tool(app, token=token, name=name, arguments=arguments))["result"]
        assert result["isError"] is True  # type: ignore[index]
        return str(result["content"])  # type: ignore[index]

    # Reaches the Teams branch, which refuses a token with no Teams user bound.
    assert "teams-bound identity" in await call(
        "send_message", {"channel_id": _THREAD, "content": "hi"}
    )
    assert "teams-bound identity" in await call("list_channels", {})
    # Untagged for Teams: hidden, so the registry does not know it.
    assert "Unknown tool" in await call("rename_thread", {"thread_id": _THREAD, "name": "x"})
