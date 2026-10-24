"""Agent-bound GitHub connect requests from a conversation."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import github_connect as connect_tool
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_connect_cards import build_connect_card
from daimon.core.github_credentials import build_multifernet
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import digest, get_invitation
from daimon.testing.factories import make_account, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: "1:1" as the chat, not inside a time such as "21:15".
_ONE_TO_ONE = re.compile(r"(?<![\d:])1:1(?![\d:])")


@pytest.mark.asyncio
async def test_discord_mention_card_has_no_url_and_slack_mention_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Thread:
        guild = SimpleNamespace(id=123)
        send = AsyncMock()

    thread = Thread()
    rest = SimpleNamespace(fetch_channel=AsyncMock(return_value=thread))

    @asynccontextmanager
    async def client_context(_token: str):  # type: ignore[no-untyped-def]
        yield rest

    monkeypatch.setattr(connect_tool.discord, "Thread", Thread)
    monkeypatch.setattr(connect_tool, "rest_client", client_context)

    def bot_token(_runtime: McpRuntime) -> str:
        return "bot-token"

    monkeypatch.setattr(connect_tool, "_require_bot_token", bot_token)
    card = build_connect_card(
        agent_name="ResearchBot",
        identity_enabled=True,
        avatar_url="https://mcp.test/avatars/research.png",
        public_base_url="https://mcp.test",
    )
    monkeypatch.setattr(connect_tool, "resolve_connect_card", AsyncMock(return_value=card))
    runtime = SimpleNamespace(session_factory=object(), settings=object())
    discord_auth = SimpleNamespace(external_id="123", tenant_id=uuid.uuid4())
    await connect_tool._post_discord_connect_card(  # pyright: ignore[reportPrivateUsage]
        runtime,  # type: ignore[arg-type]
        discord_auth,  # type: ignore[arg-type]
        thread_id="789",
        requester_id="456",
        intent_id=uuid.UUID(int=1),
        agent_name="ResearchBot",
    )
    kwargs = thread.send.await_args.kwargs
    assert thread.send.await_args.args == ()
    assert kwargs["embed"].to_dict()["author"]["name"] == "ResearchBot"
    assert (
        kwargs["embed"].description
        == "Nothing is connected yet.\n\nTap the button and tick the repos ResearchBot can use."
    )
    assert kwargs["embed"].to_dict()["color"] == 0x0C1F40
    item = kwargs["view"].children[0]
    assert item.custom_id == f"gh_connect:456:{uuid.UUID(int=1).hex}"
    assert item.emoji.name == "🔗"
    assert item.url is None

    slack_client = SimpleNamespace(chat_postEphemeral=AsyncMock(), conversations_open=AsyncMock())
    monkeypatch.setattr(connect_tool, "slack_web_client", AsyncMock(return_value=slack_client))
    slack_auth = SimpleNamespace(external_id="T1", tenant_id=uuid.uuid4())
    await connect_tool._post_slack_connect_card(  # pyright: ignore[reportPrivateUsage]
        runtime,  # type: ignore[arg-type]
        slack_auth,  # type: ignore[arg-type]
        channel_id="C1",
        thread_id="123.456",
        requester_id="U1",
        url="https://mcp.test/private-link",
        agent_name="ResearchBot",
    )
    slack_client.chat_postEphemeral.assert_awaited_once()
    slack_client.conversations_open.assert_not_awaited()
    sent = slack_client.chat_postEphemeral.await_args.kwargs
    assert (sent["channel"], sent["thread_ts"], sent["user"]) == ("C1", "123.456", "U1")
    assert "https://" not in sent["text"]
    attachment = sent["attachments"][0]
    assert attachment["color"] == "#0C1F40"
    button = next(block for block in attachment["blocks"] if block["type"] == "actions")[
        "elements"
    ][0]
    assert button["url"] == "https://mcp.test/private-link"
    assert button["text"] == {
        "type": "plain_text",
        "text": "🔗 Connect GitHub",
        "emoji": True,
    }


class _FakeTeams:
    """Bot Framework for one requester: a roster lookup, a 1:1 chat and its posts."""

    def __init__(self, *, chat_status: int = 201) -> None:
        self.chat_status = chat_status
        self.posts: list[tuple[str, dict[str, object]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/members/" in request.url.path:
            aad = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": f"29:{aad}", "aadObjectId": aad})
        body = json.loads(request.content)
        self.posts.append((request.url.path.split("/v3/", 1)[1], body))
        if request.url.path.endswith("/v3/conversations"):
            return httpx.Response(self.chat_status, json={"id": "a:dm-1"})
        return httpx.Response(200, json={"id": "m-1"})


@pytest.mark.asyncio
async def test_teams_connect_link_goes_to_the_requesters_one_to_one_chat(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link is a bearer link: Teams gets it in a 1:1 card, never in the channel."""
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"teams-{uuid.uuid4().hex[:8]}")
        account = await make_account(session, tenant=tenant)
        await set_role(session, account.id, Role.ADMIN)
    fake = _FakeTeams()
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=AsyncMock(),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
            mcp=McpSettings(public_url=HttpUrl("https://mcp.test/mcp")),
            github_app=GithubAppSettings(
                app_id="42",
                app_slug="sample-app",
                private_key=SecretStr("pem"),
                client_id="client",
                client_secret=SecretStr("secret"),
            ),
        ),
        deployment_default=DeploymentDefault(),
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        teams_client=TeamsBotClient(
            httpx.AsyncClient(transport=httpx.MockTransport(fake)),
            client_id="app",
            client_secret="secret",
            tenant_id="t",
        ),
    )
    origin = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="19:c@thread.tacv2",
        thread_id="19:c@thread.tacv2;messageid=1",
        sender_foreign=False,
    )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(connect_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="teams",
        external_id="entra-tenant",
        platform_user_id="aad-admin",
        is_admin=True,
        is_platform_admin=True,
    )
    sent = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, origin_context_id=str(uuid.uuid4())
    )
    assert sent.status == "sent", "an admin's Teams connect should deliver"
    assert "1:1 chat" in sent.message and "http" not in sent.message, (
        "the channel reply says where the link went, never the link"
    )
    assert [path for path, _ in fake.posts] == [
        "conversations",
        "conversations/a:dm-1/activities",
    ], "the card should go to a freshly opened 1:1 chat, not the channel"
    card = fake.posts[1][1]["attachments"][0]["content"]  # type: ignore[index]
    [action] = card["actions"]
    assert action["type"] == "Action.OpenUrl", "the link should be a button"
    token = action["url"].removeprefix("https://mcp.test/oauth/github/connect/")
    async with committing_sessionmaker() as session:
        invitation = await get_invitation(session, digest(token))
    assert invitation is not None and invitation.origin_platform == "teams", (
        "the button should carry a live Teams invitation"
    )

    fake.chat_status = 403
    failed = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, origin_context_id=str(uuid.uuid4())
    )
    assert failed.status == "delivery_failed", "a refused 1:1 chat should fail the delivery"
    assert "1:1 chat" in failed.message, "the error should say the 1:1 chat failed"
    async with committing_sessionmaker() as session:
        live = await session.scalar(
            text("SELECT count(*) FROM github_connect_invitations WHERE tenant_id = :tenant"),
            {"tenant": tenant.id},
        )
    assert live == 1, "the undelivered invitation should be revoked"

    origin.sender_foreign = True  # a guest from another organisation has no 1:1 chat to open
    guest = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, origin_context_id=str(uuid.uuid4())
    )
    assert guest.status == "delivery_failed", "a guest's card cannot be delivered either"
    assert "organisation that added Daimon" in guest.message, "the guest is told who it is for"
    assert _ONE_TO_ONE.search(guest.message) is None and "chat" not in guest.message, guest.message
