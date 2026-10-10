"""Agent-bound GitHub connect requests from a conversation."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import github_connect as connect_tool
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_connect_cards import build_connect_card
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.github_grant_proposals import resolve as resolve_grant_proposal
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_confirmed_connected_repo_is_granted_in_chat(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"grant-{uuid.uuid4().hex[:8]}")
        account = await make_account(session, tenant=tenant)
        await set_role(session, account.id, Role.ADMIN)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_agent")
        await github_app_installations.upsert(
            session,
            installation_id=98765,
            account_login="owner",
            repo_full_names=["owner/repo"],
        )
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos "
                "(tenant_id, repo_id, scope_agent_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id, "
                "authorized_by_account_id) "
                "VALUES (:tenant, 12345, :agent, 12, 98765, 'owner/repo', "
                "'write', 17, :account)"
            ),
            {"tenant": tenant.id, "agent": agent_id, "account": account.id},
        )
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
    )
    origin = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC) - timedelta(seconds=1),
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="channel",
        thread_id="thread",
    )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(connect_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_discord_connect_card", delivery)
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id="workspace",
        platform_user_id="admin",
        is_admin=True,
    )
    proposed = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, origin_context_id=str(uuid.uuid4()), repo_name="owner/repo"
    )
    assert proposed.status == "proposed"
    assert proposed.message == "Give Agent read access to owner/repo?"
    same_turn = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, origin_context_id=str(uuid.uuid4()), repo_name="owner/repo", confirmed=True
    )
    assert same_turn.status == "proposed"
    async with committing_sessionmaker.begin() as session:
        other_account = await make_account(session, tenant=tenant)
        await set_role(session, other_account.id, Role.ADMIN)
    other = replace(auth, account_id=other_account.id, platform_user_id="other")
    origin.id = uuid.uuid4()
    origin.created_at = datetime.now(UTC) + timedelta(seconds=1)
    other_turn = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, other, origin_context_id=str(uuid.uuid4()), repo_name="owner/repo", confirmed=True
    )
    assert other_turn.status == "proposed"
    no_human_yes = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, origin_context_id=str(uuid.uuid4()), repo_name="owner/repo", confirmed=True
    )
    assert no_human_yes.status == "proposed"
    async with committing_sessionmaker.begin() as session:
        await resolve_grant_proposal(
            session,
            origin=SimpleNamespace(
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                thread_id="thread",
                id=origin.id,
                created_at=origin.created_at,
            ),
            message_text="yes",
        )  # type: ignore[arg-type]
    result = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
        confirmed=True,
    )
    assert result.status == "granted"
    assert result.message == "Agent has read access to owner/repo."
    delivery.assert_not_awaited()
    async with committing_sessionmaker() as session:
        assert (
            await github_access.get_agent_mode(session, tenant_id=tenant.id, agent_id=agent_id)
            == "app"
        )
        [grant] = await github_access.list_agent_grants(
            session, tenant_id=tenant.id, agent_id=agent_id
        )
        assert (grant.baseline_access, grant.ceiling_access, grant.staged) == (
            "read",
            "read",
            False,
        )
    write_proposal = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
        required_ability="write",
    )
    assert write_proposal.message == "Give Agent read and change access to owner/repo?"
    origin.id = uuid.uuid4()
    origin.created_at = datetime.now(UTC) + timedelta(seconds=1)
    async with committing_sessionmaker.begin() as session:
        await resolve_grant_proposal(
            session,
            origin=SimpleNamespace(
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                thread_id="thread",
                id=origin.id,
                created_at=origin.created_at,
            ),
            message_text="yes",
        )  # type: ignore[arg-type]
    write = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
        required_ability="write",
        confirmed=True,
    )
    assert write.status == "granted"
    async with committing_sessionmaker() as session:
        [grant] = await github_access.list_agent_grants(
            session, tenant_id=tenant.id, agent_id=agent_id
        )
        assert grant.ceiling_access == "write"
    expired_proposal = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/missing",
    )
    assert expired_proposal.status == "proposed"
    async with committing_sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_grant_proposals SET expires_at = now() - interval '1 second' "
                "WHERE repo_name = 'owner/missing' AND requester_account_id = :account"
            ),
            {"account": auth.account_id},
        )
    origin.id = uuid.uuid4()
    origin.created_at = datetime.now(UTC) + timedelta(seconds=1)
    missing = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/missing",
        confirmed=True,
    )
    assert missing.status == "proposed"
    delivery.assert_not_awaited()
    other_agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_other")
    async with committing_sessionmaker.begin() as session:
        channel_account = await make_account(session, tenant=tenant)
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="channel",
            role_ids=[],
            user_ids=["channel-admin"],
            actor_account_id=None,
        )
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"Agent": {"runs_in": ["channel"]}}}
            ),
        )
        await github_app_installations.upsert(
            session,
            installation_id=98765,
            account_login="owner",
            repo_full_names=["owner/repo", "owner/foreign"],
        )
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos "
                "(tenant_id, repo_id, scope_agent_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id, "
                "authorized_by_account_id) VALUES "
                "(:tenant, 12346, :agent, 12, 98765, 'owner/foreign', 'read', 17, :account)"
            ),
            {"tenant": tenant.id, "agent": other_agent_id, "account": account.id},
        )
    channel_auth = AuthIdentity(
        account_id=channel_account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="channel-admin",
    )
    await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, channel_auth, origin_context_id=str(uuid.uuid4()), repo_name="owner/foreign"
    )
    origin.id = uuid.uuid4()
    origin.created_at = datetime.now(UTC) + timedelta(seconds=1)
    async with committing_sessionmaker.begin() as session:
        await resolve_grant_proposal(
            session,
            origin=SimpleNamespace(
                tenant_id=tenant.id,
                account_id=channel_account.id,
                platform="discord",
                thread_id="thread",
                id=origin.id,
                created_at=origin.created_at,
            ),
            message_text="yes",
        )  # type: ignore[arg-type]
    foreign = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        channel_auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/foreign",
        confirmed=True,
    )
    assert foreign.status == "sent"
    async with committing_sessionmaker() as session:
        assert all(
            grant.repo_id != 12346
            for grant in await github_access.list_agent_grants(
                session, tenant_id=tenant.id, agent_id=agent_id
            )
        )


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
    assert kwargs["embed"].description == "Pick repos ResearchBot can use."
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


@pytest.mark.asyncio
async def test_member_request_is_recorded_and_admin_gets_bound_thread_button(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id="workspace")
        account = await make_account(session, tenant=tenant)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
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
        fernet=fernet,
    )
    origin = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        configuration_target_name="ConnectedBot",
        configuration_target_ma_agent_id="agent_connected",
        responder_name="ResearchBot",
        responder_ma_agent_id="agent_research",
        parent_channel_id="channel",
        thread_id="thread",
    )
    agent = SimpleNamespace(id="agent_connected", name="ConnectedBot", metadata={})
    delivery = AsyncMock()
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(connect_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(connect_tool, "_post_discord_connect_card", delivery)
    member = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="123",
        is_admin=False,
    )
    result = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, member, origin_context_id=str(uuid.uuid4())
    )
    assert result.status == "ask_admin" and result.message == "Ask an admin"
    delivery.assert_not_awaited()
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    async with committing_sessionmaker() as session:
        request = (
            await session.execute(
                text(
                    "SELECT requester_account_id FROM github_connect_requests "
                    "WHERE agent_id = :agent_id"
                ),
                {"agent_id": agent_id},
            )
        ).one_or_none()
        assert request is not None and request[0] == account.id
    async with committing_sessionmaker.begin() as session:
        await set_role(session, account.id, Role.ADMIN)
    admin = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id="workspace",
        platform_user_id="123",
        is_admin=True,
    )
    result = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        admin,
        origin_context_id=str(uuid.uuid4()),
        requested_work="Review the issue after GitHub is connected",
    )
    assert result.status == "sent" and "http" not in result.message
    delivery.assert_awaited_once()
    assert delivery.await_args is not None
    assert delivery.await_args.kwargs["agent_name"] == "ConnectedBot"
    assert delivery.await_args.kwargs["requester_id"] == "123"
    assert delivery.await_args.kwargs["thread_id"] == "thread"
    assert "url" not in delivery.await_args.kwargs
    intent_id = delivery.await_args.kwargs["intent_id"]
    assert str(intent_id) not in result.model_dump_json()
    async with committing_sessionmaker() as session:
        intent = (
            await session.execute(
                text(
                    "SELECT encrypted_token, requested_work, origin_thread_id, "
                    "origin_ma_agent_id FROM github_connect_click_intents WHERE id = :id"
                ),
                {"id": intent_id},
            )
        ).one_or_none()
        assert intent is not None and intent.encrypted_token is None
        assert intent.requested_work == "Review the issue after GitHub is connected"
        assert intent.origin_thread_id == "thread"
        assert intent.origin_ma_agent_id == "agent_research"
    delivery.side_effect = ToolError("thread unavailable")
    blocked = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, admin, origin_context_id=str(uuid.uuid4())
    )
    assert blocked.status == "delivery_failed"
    assert blocked.message == "I couldn't show the GitHub connection button. Try again."
    assert delivery.await_args is not None
    blocked_id = delivery.await_args.kwargs["intent_id"]
    async with committing_sessionmaker() as session:
        assert (
            await session.scalar(
                text("SELECT id FROM github_connect_click_intents WHERE id = :id"),
                {"id": blocked_id},
            )
            is None
        )
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"ConnectedBot": {"runs_in": ["client-channel"]}}}
            ),
        )
    delivery.reset_mock()
    delivery.side_effect = None
    # A pinned agent with no server-wide repos may get repos of its own.
    pinned = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, admin, origin_context_id=str(uuid.uuid4())
    )
    assert pinned.status == "sent"
    delivery.assert_awaited_once()

    # So may a channel admin of the only channel it runs in, who is not a server admin.
    async with committing_sessionmaker.begin() as session:
        channel_admin = await make_account(session, tenant=tenant)
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="client-channel",
            role_ids=[],
            user_ids=["777"],
            actor_account_id=None,
        )
    delivery.reset_mock()
    channel_admin_auth = AuthIdentity(
        account_id=channel_admin.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="777",
        is_admin=False,
    )
    own = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, channel_admin_auth, origin_context_id=str(uuid.uuid4())
    )
    assert own.status == "sent"
    allowed_proposal = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        channel_admin_auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
    )
    assert allowed_proposal.status == "proposed"
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"ConnectedBot": {"runs_in": ["other-channel"]}}}
            ),
        )
    origin.id = uuid.uuid4()
    origin.created_at = datetime.now(UTC) + timedelta(seconds=1)
    async with committing_sessionmaker.begin() as session:
        await resolve_grant_proposal(
            session,
            origin=SimpleNamespace(
                tenant_id=tenant.id,
                account_id=channel_admin.id,
                platform="discord",
                thread_id="thread",
                id=origin.id,
                created_at=origin.created_at,
            ),
            message_text="yes",
        )  # type: ignore[arg-type]
    other_channel = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        channel_admin_auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
        confirmed=True,
    )
    assert other_channel.status == "ask_admin"
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate(
                {"agent_rules": {"ConnectedBot": {"runs_in": ["client-channel"]}}}
            ),
        )
    assert delivery.await_args is not None
    async with committing_sessionmaker() as session:
        bound = (
            await session.execute(
                text(
                    "SELECT requester_account_id, agent_ma_id "
                    "FROM github_connect_click_intents WHERE id = :id"
                ),
                {"id": delivery.await_args.kwargs["intent_id"]},
            )
        ).one()
        assert (bound.requester_account_id, bound.agent_ma_id) == (
            channel_admin.id,
            "agent_connected",
        )
    # A managed agent stays a server admin's.
    agent.metadata = {"daimon_managed": "true"}
    delivery.reset_mock()
    managed = await connect_tool._github_connect_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        channel_admin_auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
    )
    assert managed.status == "ask_admin"
    delivery.assert_not_awaited()
