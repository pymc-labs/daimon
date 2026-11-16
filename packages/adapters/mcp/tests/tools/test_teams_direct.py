"""Teams direct messages over a fake Bot Framework: who can be reached, and where it lands."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.direct_messages import set_dm_agent_name
from daimon.core.stores.dm_access import set_grant
from daimon.core.stores.domain import Role
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CALLER = "11111111-2222-3333-4444-555555555555"
_RECIPIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_TEAM = "19:team@thread.tacv2"


class _Fake:
    def __init__(
        self,
        roster: set[str],
        *,
        chat_status: int = 201,
        tenant: str | None = None,
        teams: dict[str, list[str]] | None = None,
    ) -> None:
        self.roster, self.chat_status, self.tenant = roster, chat_status, tenant
        self.teams = teams or {}
        self.posts: list[tuple[str, dict[str, object]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        path = request.url.path
        if request.url.host == "graph.microsoft.com":
            group = path.split("/teams/", 1)[1].split("/", 1)[0]
            if group not in self.teams:
                return httpx.Response(403)
            return httpx.Response(200, json={"value": [{"userId": u} for u in self.teams[group]]})
        if "/members/" in path:
            aad = path.rsplit("/", 1)[1]
            if aad in self.roster:
                member = {"id": f"29:{aad}", "aadObjectId": aad}
                if self.tenant is not None:
                    member["tenantId"] = self.tenant
                return httpx.Response(200, json=member)
            return httpx.Response(404)
        if request.method == "POST":
            body = json.loads(request.content)
            self.posts.append((path, body))
            if path.endswith("/v3/conversations"):
                return httpx.Response(self.chat_status, json={"id": "a:dm-1"})
            return httpx.Response(200, json={"id": f"m-{len(self.posts)}"})
        return httpx.Response(404)


async def _call(
    fake: _Fake,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    granted_team: str | None = None,
    **kw: str,
):
    tenant = await make_tenant(db_session)
    if granted_team is not None:
        await set_dm_agent_name(db_session, tenant_id=tenant.id, agent_name="ada")
        await set_grant(
            db_session, tenant_id=tenant.id, agent_name="ada", who="listed", user_ids=[],
            group_ids=[granted_team], set_by=None, now=datetime(2026, 1, 1, tzinfo=UTC),
        )  # fmt: skip
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


async def test_direct_message_uses_plain_text(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    fake = _Fake({_CALLER, _RECIPIENT})
    await _call(fake, db_session, sessionmaker)
    posted = fake.posts[1][1]
    assert posted["text"] == "hello"
    assert "attachments" not in posted


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


async def test_a_recipient_from_another_organisation_gets_no_dm_attempt(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    fake = _Fake({_CALLER, _RECIPIENT}, tenant="other")
    with pytest.raises(ToolError, match="another organisation"):
        await _call(fake, db_session, sessionmaker)
    assert fake.posts == [], "no 1:1 chat is opened"


async def test_a_roster_entry_naming_our_own_tenant_is_reached_as_before(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    fake = _Fake({_CALLER, _RECIPIENT}, tenant="T")
    assert (await _call(fake, db_session, sessionmaker)).channel_id == "a:dm-1"


@pytest.mark.parametrize(
    ("teams", "sent"),
    [({"group-1": [_RECIPIENT]}, True), ({"group-1": [_CALLER]}, False), ({}, False)],
)
async def test_teams_dm_when_the_grant_names_a_team_then_its_roster_decides(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    teams: dict[str, list[str]],
    sent: bool,
) -> None:
    """A Teams DM follows the sender's grant by team; a roster Graph refuses denies."""
    fake = _Fake({_CALLER, _RECIPIENT}, teams=teams)
    if sent:
        result = await _call(fake, db_session, sessionmaker, granted_team="group-1")
        assert result.channel_id == "a:dm-1", "a team member is reached"
        return
    with pytest.raises(ToolError, match="may not send"):
        await _call(fake, db_session, sessionmaker, granted_team="group-1")
    assert not fake.posts, "no chat is opened for a person the grant leaves out"
