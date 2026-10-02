"""Teams direct messages over a fake Bot Framework: who can be reached, and where it lands."""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CALLER = "11111111-2222-3333-4444-555555555555"
_RECIPIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_TEAM = "19:team@thread.tacv2"


class _Fake:
    def __init__(self, roster: set[str], *, chat_status: int = 201) -> None:
        self.roster, self.chat_status = roster, chat_status
        self.posts: list[tuple[str, dict[str, object]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        path = request.url.path
        if "/members/" in path:
            aad = path.rsplit("/", 1)[1]
            if aad in self.roster:
                return httpx.Response(200, json={"id": f"29:{aad}", "aadObjectId": aad})
            return httpx.Response(404)
        if request.method == "POST":
            body = json.loads(request.content)
            self.posts.append((path, body))
            if path.endswith("/v3/conversations"):
                return httpx.Response(self.chat_status, json={"id": "a:dm-1"})
            return httpx.Response(200, json={"id": f"m-{len(self.posts)}"})
        return httpx.Response(404)


async def _call(
    fake: _Fake, db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], **kw: str
):
    tenant = await make_tenant(db_session)
    await record_teams_installation(
        db_session, tenant_id=tenant.id, team_id=_TEAM, group_id=str(uuid.uuid4()), name="Lab"
    )
    client = TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        client_id="app",
        client_secret="secret",
        tenant_id="t",
    )
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]  # any recipient allowed
        deployment_default=DeploymentDefault(),
        teams_client=client,
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=Role.USER,
        platform="teams",
        platform_user_id=_CALLER,
    )
    args = {"recipient_id": _RECIPIENT.upper(), "content": "hello"} | kw
    return await send_direct_message_impl(runtime, auth, **args)


async def test_a_dm_opens_the_recipients_1_1_chat_and_posts_there(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    fake = _Fake({_CALLER, _RECIPIENT})
    result = await _call(fake, db_session, sessionmaker)
    assert (result.platform, result.recipient_id, result.channel_id) == (
        "teams",
        _RECIPIENT,
        "a:dm-1",
    )
    (opened_path, opened), (posted_path, posted) = fake.posts
    assert opened["isGroup"] is False and opened["members"] == [{"id": f"29:{_RECIPIENT}"}]
    assert posted_path.endswith("/a:dm-1/activities") and posted["text"] == "hello"
    assert result.message_ids == ["m-2"]


@pytest.mark.parametrize("roster", [{_CALLER}, {_RECIPIENT}])
async def test_both_people_must_share_a_team_with_daimon(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], roster: set[str]
) -> None:
    fake = _Fake(roster)
    with pytest.raises(ToolError, match="both be members of a team daimon is in"):
        await _call(fake, db_session, sessionmaker)
    assert fake.posts == []


async def test_a_refused_chat_says_nothing_was_sent(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ToolError, match="failed after 0 message"):
        await _call(_Fake({_CALLER, _RECIPIENT}, chat_status=403), db_session, sessionmaker)


async def test_a_teams_recipient_must_be_an_entra_id(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ToolError, match="user ID"):
        await _call(_Fake(set()), db_session, sessionmaker, recipient_id="29:abc")
