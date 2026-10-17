"""Linked-grant journey with synthetic GitHub responses."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import discord
import httpx
import pytest
import pytest_asyncio
import structlog
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.oauth_github_personal import build_personal_link_routes
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import github_connect as connect_tool
from daimon.adapters.mcp.tools import github_linked_grant as linked
from daimon.adapters.mcp.tools import github_linked_pending as linked_pending
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    GithubAppSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_panel import requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.native_admins import AdminAttestationUnavailable
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import (
    SecondInstallationError,
    list_agent_grants,
    list_authorized_repos,
    require_owner_grant_installations,
    stage_grant,
)
from daimon.core.stores.github_app_installations import get as get_installation
from daimon.core.stores.github_installation_reconciliation import get as get_reconciliation
from daimon.core.stores.github_linked_pending import (
    CLAIM_LEASE,
    CLAIMED_GARBAGE_AFTER,
    ClaimLostError,
    renew,
)
from daimon.core.stores.github_linked_pending import resolve as resolve_pending
from daimon.core.stores.github_linked_pending import take as take_pending
from daimon.core.stores.github_links import get_account_link, save_verified_link, unlink_account
from daimon.core.stores.github_owner_grants import (
    exclude_repo,
    list_owner_grants,
    upsert_owner_grant,
)
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from starlette.applications import Starlette
from starlette.routing import Route


def _visible(page_html: str) -> str:
    """The text a person reads: tags removed, since names sit in nowrap spans."""
    return re.sub(r"<[^>]+>", "", page_html)


@pytest.fixture
def github_http(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        permission="push",
        repo_visible=True,
        refresh_fail=False,
        installation_suspended=False,
        installation_detail_status=None,
        second_installation=False,
        during_repo_get=None,
    )
    original_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={"error": "bad_refresh_token"}
                if state.refresh_fail
                else {
                    "access_token": "rotated",
                    "refresh_token": "new-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 100000,
                },
            )
        permissions = {
            key: key == state.permission for key in ("admin", "maintain", "push", "triage", "pull")
        }
        if request.url.path == "/user/installations":
            installations = [
                {
                    "id": 201,
                    "account": {"id": 12, "login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                }
            ]
            if state.second_installation:
                installations.append(
                    {
                        "id": 202,
                        "account": {"id": 13, "login": "gadgets", "type": "Organization"},
                        "repository_selection": "selected",
                    }
                )
            return httpx.Response(200, json={"installations": installations})
        if request.url.path == "/app/installations/202":
            return httpx.Response(
                200,
                json={
                    "id": 202,
                    "account": {"id": 13, "login": "gadgets", "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": None,
                    "permissions": {
                        "metadata": "read",
                        "contents": "write",
                        "issues": "write",
                        "pull_requests": "write",
                    },
                },
            )
        if request.url.path == "/user/installations/202/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 102,
                            "full_name": "gadgets/tools",
                            "owner": {"id": 13, "login": "gadgets"},
                            "permissions": permissions,
                        }
                    ]
                },
            )
        if request.url.path == "/repos/gadgets/tools":
            return httpx.Response(
                200,
                json={
                    "id": 102,
                    "full_name": "gadgets/tools",
                    "owner": {"id": 13},
                    "permissions": permissions,
                },
            )
        if request.url.path == "/app/installations/201":
            if state.installation_detail_status is not None:
                return httpx.Response(state.installation_detail_status)
            return httpx.Response(
                200,
                json={
                    "id": 201,
                    "account": {"id": 12, "login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": (
                        "2026-10-16T00:00:00Z" if state.installation_suspended else None
                    ),
                    "permissions": {
                        "metadata": "read",
                        "contents": "write",
                        "issues": "write",
                        "pull_requests": "write",
                    },
                },
            )
        if request.url.path.endswith("/repositories"):
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 101,
                            "full_name": "acme/widgets",
                            "owner": {"id": 12, "login": "acme"},
                            "permissions": permissions,
                        }
                    ]
                    if state.repo_visible
                    else []
                },
            )
        if request.url.path == "/repos/acme/widgets":
            if not state.repo_visible:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": 101,
                    "full_name": "acme/widgets",
                    "owner": {"id": 12},
                    "permissions": permissions,
                },
            )
        raise AssertionError(request.url)

    async def respond_async(request: httpx.Request) -> httpx.Response:
        # Lets a test change state while the repository permission GET is outstanding.
        if request.url.path == "/repos/acme/widgets" and state.during_repo_get is not None:
            await state.during_repo_get()
        return respond(request)

    monkeypatch.setattr(
        linked.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond_async)),
    )
    monkeypatch.setattr(linked, "build_app_jwt", lambda *_args, **_kwargs: "app-jwt")
    state.original_client = original_client
    state.respond = respond
    return state


@pytest.fixture(autouse=True)
def live_admin(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The resume path's live platform admin read; a test sets `return_value` or `side_effect`."""
    attest = AsyncMock(return_value=True)
    monkeypatch.setattr(linked_pending, "_attest_native_admin", attest)
    return attest


@pytest_asyncio.fixture
async def setup_linked(committing_sessionmaker: async_sessionmaker[AsyncSession]):
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"acme-{uuid.uuid4().hex[:8]}")
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_agent")
        account = await make_account(session, tenant=tenant)
        await set_role(session, account.id, Role.ADMIN)
        await make_platform_principal(
            session, platform="discord", external_id="person", tenant=tenant, account=account
        )
        await session.execute(
            text(
                "INSERT INTO github_app_installations "
                "(installation_id, account_login, app, account_id, account_type, "
                "repository_selection, repo_full_names) VALUES "
                "(201, 'acme', 'github_app', 12, 'Organization', 'selected', "
                "ARRAY['acme/base', 'acme/widgets'])"
            )
        )
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos "
                "(tenant_id, repo_id, scope_agent_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id, "
                "authorized_by_account_id, status) VALUES "
                "(:tenant_id, 100, :agent_id, 12, 201, 'acme/base', 'read', 300, "
                ":account_id, 'active')"
            ),
            {"tenant_id": tenant.id, "agent_id": agent_id, "account_id": account.id},
        )
        await save_verified_link(
            session,
            intent_account_id=account.id,
            platform="discord",
            platform_user_id="person",
            github_user_id=300,
            login="person",
            access_token="synthetic",
            refresh_token="synthetic-refresh",
            expires_in=3600,
            refresh_expires_in=86400,
            fernet=fernet,
        )
        assert await get_account_link(session, account_id=account.id) is not None

    async def manages(session: AsyncSession) -> bool:
        current = await get_account(session, account.id)
        return current is not None and current.role == Role.ADMIN

    async def grant(*, account_id: uuid.UUID | None = None, repo_name: str = "acme/widgets"):
        return await linked.linked_grant(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=account_id or account.id,
            agent_id=agent_id,
            agent_name="Agent",
            repo_name=repo_name,
            fernet=fernet,
            client_id="synthetic",
            client_secret="synthetic",
            app_id="42",
            private_key="synthetic",
            manages_now=manages,
        )

    return SimpleNamespace(
        sessionmaker=committing_sessionmaker,
        tenant=tenant,
        account=account,
        agent_id=agent_id,
        grant=grant,
        fernet=fernet,
    )


@pytest.mark.asyncio
async def test_tool_uses_verified_turn_account_and_private_link(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
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
        fernet=setup_linked.fernet,
    )
    auth = AuthIdentity(
        account_id=setup_linked.account.id,
        tenant_id=setup_linked.tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id=setup_linked.tenant.external_id,
        platform_user_id="person",
        is_admin=True,
    )
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="123",
        thread_id="456",
    )
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(
        connect_tool,
        "resolve_setup_agent",
        AsyncMock(
            return_value=SimpleNamespace(
                id="ma_agent",
                name="Agent",
                metadata={},
            )
        ),
    )
    post_link = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_personal_link", post_link)
    github_http.permission = "pull"
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets"
    )
    assert result.status == "granted_read_only"
    assert result.message == (
        "Agent can now read acme/widgets.\n"
        "Everyone who talks to Agent can use this repo through Agent."
    )
    post_link.assert_not_awaited()
    async with setup_linked.sessionmaker() as session:
        repo = next(
            row
            for row in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if row.repo_id == 101
        )
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (repo.max_access, grant.ceiling_access) == ("read", "read")
    github_http.permission = "push"
    upgraded = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets", access="write"
    )
    assert upgraded.status == "granted"
    assert upgraded.message == (
        "Agent can now read and change acme/widgets.\n"
        "Everyone who talks to Agent can use this repo through Agent."
    )
    async with setup_linked.sessionmaker() as session:
        repo = next(
            row
            for row in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if row.repo_id == 101
        )
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (repo.max_access, grant.ceiling_access) == ("write", "write")
    github_http.permission = "pull"
    limited = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets", access="write"
    )
    assert limited.status == "granted_read_only"
    assert "read and change" not in limited.message
    async with setup_linked.sessionmaker() as session:
        repo = next(
            row
            for row in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if row.repo_id == 101
        )
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (repo.max_access, grant.ceiling_access) == ("read", "read")
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/new"
    )
    assert result.status == "needs_install"
    assert result.message == (
        "Daimon can't reach acme/new with your GitHub account.\n"
        "If you can open it on GitHub, ask an owner or repo admin to add it to the Daimon app."
    )
    assert "http" not in result.message and "github.com" not in result.message
    assert post_link.await_args.kwargs["label"] == "Open GitHub setup"
    post_link.reset_mock()
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE github_user_links SET status = 'broken' WHERE github_user_id = 300")
        )
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/new"
    )
    assert result.status == "relink"
    assert result.message == "Your GitHub link needs renewing."
    assert "http" not in result.message
    post_link.assert_awaited_once()
    assert "/oauth/github/link/" in post_link.await_args.kwargs["url"]
    post_link.reset_mock()
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/new"
    )
    assert result.status == "needs_link"
    assert result.message == "Link GitHub to check your repo access."
    assert "http" not in result.message
    post_link.assert_awaited_once()
    assert "/oauth/github/link/" in post_link.await_args.kwargs["url"]


@pytest.mark.asyncio
async def test_linked_manager_grants_without_browser(setup_linked, github_http) -> None:
    result = await setup_linked.grant()
    assert result.status == "granted"
    async with setup_linked.sessionmaker() as session:
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (grant.repo_id, grant.ceiling_access) == (101, "write")
        repo = next(
            r
            for r in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if r.repo_id == 101
        )
        assert (
            repo.scope_agent_id,
            repo.authorized_by_github_user_id,
            repo.authorized_permission,
        ) == (setup_linked.agent_id, 300, "push")
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        assert (
            len(
                await list_agent_grants(
                    session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
                )
            )
            == 1
        )
        reconciliation = await get_reconciliation(session, installation_id=201)
        assert reconciliation is not None
        assert (reconciliation.state, reconciliation.generation) == ("pending", 2)


@pytest.mark.asyncio
async def test_explicit_single_grant_clears_owner_exclusion(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await exclude_repo(
            session,
            tenant_id=setup_linked.tenant.id,
            agent_id=setup_linked.agent_id,
            repo_full_name="acme/widgets",
        )
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        excluded = await session.scalar(
            text(
                "SELECT count(*) FROM agent_github_owner_exclusions WHERE tenant_id = :tenant "
                "AND agent_id = :agent AND repo_full_name = 'acme/widgets'"
            ),
            {"tenant": setup_linked.tenant.id, "agent": setup_linked.agent_id},
        )
        assert excluded == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
async def test_relink_during_github_lookup_rejects_old_token_evidence(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch, repo_name: str
) -> None:
    if repo_name.endswith("/*"):
        async with setup_linked.sessionmaker.begin() as session:
            await session.execute(
                text(
                    "UPDATE github_app_installations SET repo_full_names = "
                    "ARRAY['acme/widgets'] WHERE installation_id = 201"
                )
            )
    original_installations = linked._installations

    async def relink_during_lookup(client: httpx.AsyncClient, token: str):
        async with setup_linked.sessionmaker.begin() as session:
            await save_verified_link(
                session,
                intent_account_id=setup_linked.account.id,
                platform="discord",
                platform_user_id="person",
                github_user_id=300,
                login="person",
                access_token="new-token",
                refresh_token="new-refresh",
                expires_in=3600,
                refresh_expires_in=86400,
                fernet=setup_linked.fernet,
            )
        return await original_installations(client, token)

    monkeypatch.setattr(linked, "_installations", relink_during_lookup)

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name=repo_name,
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
    )
    assert result.status == "changed_try_again"
    async with setup_linked.sessionmaker() as session:
        assert not await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert not await list_owner_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )


@pytest.mark.asyncio
async def test_owner_first_use_associates_existing_installation(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(text("DELETE FROM tenant_github_repos WHERE installation_id = 201"))
        await session.execute(
            text("DELETE FROM github_app_installations WHERE installation_id = 201")
        )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/201/access_tokens":
            return httpx.Response(201, json={"token": "listing-token"})
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "repositories": [{"full_name": "acme/widgets"}],
                },
            )
        return github_http.respond(request)

    monkeypatch.setattr(
        linked.httpx,
        "AsyncClient",
        lambda **kwargs: github_http.original_client(transport=httpx.MockTransport(respond)),
    )

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/*",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
    )
    assert result.status == "granted_read_only"
    async with setup_linked.sessionmaker() as session:
        assert await get_installation(session, installation_id=201) is not None
        assert [
            grant.owner_login
            for grant in await list_owner_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
        ] == ["acme"]
        assert await get_reconciliation(session, installation_id=201) is not None


@pytest.mark.asyncio
async def test_resume_result_posts_to_originating_slack_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    monkeypatch.setattr(linked_pending, "slack_web_client", AsyncMock(return_value=client))
    pending = SimpleNamespace(
        platform="slack",
        workspace_id="team",
        parent_channel_id="channel",
        thread_id="thread",
        platform_user_id="person",
    )
    await linked_pending._deliver_result(SimpleNamespace(), pending, "Grant completed.")
    client.chat_postMessage.assert_awaited_once_with(
        channel="channel", thread_ts="thread", text="Grant completed."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("removed", [[], ["base"], ["base", "widgets"]])
@pytest.mark.parametrize(
    "owner_type,login,second_permission,access,expected",
    [
        ("User", "acme", "none", "read", "granted_read_only"),
        ("Organization", "person", "pull", "read", "granted_read_only"),
        ("Organization", "person", "none", "read", "no_github_access"),
        ("Organization", "person", "pull", "write", "no_github_access"),
        ("Organization", "person", "push", "write", "granted"),
    ],
)
async def test_owner_grant_requires_full_owner_authority(
    setup_linked,
    github_http,
    monkeypatch: pytest.MonkeyPatch,
    owner_type: str,
    login: str,
    second_permission: str,
    access: str,
    expected: str,
    removed: list[str],
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE github_user_links SET login = :login WHERE github_user_id = 300"),
            {"login": login},
        )
        for name in removed:
            await exclude_repo(
                session,
                tenant_id=setup_linked.tenant.id,
                agent_id=setup_linked.agent_id,
                repo_full_name=f"acme/{name}",
            )
    original = github_http.original_client

    def respond(request: httpx.Request) -> httpx.Response:
        response = github_http.respond(request)
        if request.url.path == "/user/installations":
            body = response.json()
            body["installations"][0]["account"]["type"] = owner_type
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/repositories"):
            body = response.json()
            permissions = {
                key: key == second_permission
                for key in ("admin", "maintain", "push", "triage", "pull")
            }
            body["repositories"].append(
                {
                    "id": 100,
                    "full_name": "acme/base",
                    "owner": {"id": 12, "login": "acme"},
                    "permissions": permissions,
                }
            )
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr(
        linked.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond)),
    )

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/*",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
        requested_access=access,
    )
    assert result.status == expected
    assert "http" not in result.message
    if expected == "no_github_access":
        assert result.message == (
            "You need access to every acme repo the Daimon app covers.\n"
            "Ask a server admin who can open them on GitHub."
        )
    else:
        ability = "read and change" if access == "write" else "read"
        status_line = f"Agent can now {ability} acme repos the Daimon app covers"
        status_line += f", except {len(removed)} you removed." if removed else "."
        assert result.message.split("\n") == [
            status_line,
            "New repos the app covers later join automatically. "
            "Everyone who talks to Agent can use them through Agent.",
            *([f"Still removed: {', '.join(removed)}"] if removed else []),
        ]
    async with setup_linked.sessionmaker() as session:
        grants = await list_owner_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert [grant.access for grant in grants] == (
            [access] if expected.startswith("granted") else []
        )


@pytest.mark.asyncio
async def test_owner_grant_manager_revoked_before_commit(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_app_installations SET repo_full_names = ARRAY['acme/widgets'] WHERE installation_id = 201"
            )
        )

    async def revoke() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            await set_role(session, setup_linked.account.id, Role.USER)

    monkeypatch.setattr(linked, "_before_commit", revoke)

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/*",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
    )
    assert result.status == "changed_try_again"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission,status,access",
    [
        ("admin", "granted", "write"),
        ("maintain", "granted", "write"),
        ("pull", "granted_read_only", "read"),
        ("triage", "granted_read_only", "read"),
        ("none", "no_github_access", None),
    ],
)
async def test_proposed_permission_policy(
    setup_linked, github_http, permission, status, access
) -> None:
    github_http.permission = permission
    assert (await setup_linked.grant()).status == status
    async with setup_linked.sessionmaker() as session:
        grants = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert [g.ceiling_access for g in grants] == ([access] if access else [])


@pytest.mark.asyncio
async def test_no_github_access_does_not_grant(setup_linked, github_http) -> None:
    github_http.permission = "none"
    assert (await setup_linked.grant()).status == "no_github_access"


@pytest.mark.asyncio
async def test_unlinked_or_other_account_needs_link(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        other = await make_account(session, tenant=setup_linked.tenant)
    assert (await setup_linked.grant(account_id=other.id)).status == "needs_link"
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    assert (await setup_linked.grant()).status == "needs_link"


@pytest.mark.asyncio
async def test_other_tenant_link_is_not_used(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        other_tenant = await make_tenant(session, workspace_id=f"foreign-{uuid.uuid4().hex[:8]}")
        foreign = await make_account(session, tenant=other_tenant)
        await make_platform_principal(
            session,
            platform="discord",
            external_id="foreign",
            tenant=other_tenant,
            account=foreign,
        )
        await save_verified_link(
            session,
            intent_account_id=foreign.id,
            platform="discord",
            platform_user_id="foreign",
            github_user_id=301,
            login="foreign",
            access_token="foreign-synthetic",
            refresh_token="foreign-refresh",
            expires_in=3600,
            refresh_expires_in=86400,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
        )
    assert (await setup_linked.grant(account_id=foreign.id)).status == "needs_link"


@pytest.mark.asyncio
async def test_other_tenant_installation_is_visible_to_linked_caller(
    setup_linked, github_http
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        other = await make_tenant(session, workspace_id=f"other-{uuid.uuid4().hex[:8]}")
        await session.execute(
            text("UPDATE tenant_github_repos SET tenant_id = :other WHERE tenant_id = :own"),
            {"other": other.id, "own": setup_linked.tenant.id},
        )
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        rows = await list_authorized_repos(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert any(r.repo_full_name == "acme/widgets" for r in rows)


@pytest.mark.asyncio
async def test_new_selected_installation_needs_no_prior_rows(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(text("DELETE FROM tenant_github_repos"))
        await session.execute(
            text("DELETE FROM github_app_installations WHERE installation_id = 201")
        )
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        installation = await get_installation(session, installation_id=201)
        assert installation is not None
        assert installation.repository_selection == "selected"
        assert "acme/widgets" in installation.repo_full_names


@pytest.mark.asyncio
async def test_selected_installation_needs_install(setup_linked, github_http) -> None:
    github_http.repo_visible = False
    assert (await setup_linked.grant()).status == "needs_install"


@pytest.mark.asyncio
async def test_invisible_private_repo_does_not_claim_the_app_is_missing(
    setup_linked, github_http
) -> None:
    """A repo the person cannot see looks the same as one the app does not cover."""
    github_http.repo_visible = False
    result = await setup_linked.grant()
    assert result.status == "needs_install"
    assert result.message == (
        "Daimon can't reach acme/widgets with your GitHub account.\n"
        "If you can open it on GitHub, ask an owner or repo admin to add it to the Daimon app."
    )
    assert "isn't set up" not in result.message


@pytest.mark.asyncio
async def test_suspended_installation_never_grants(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_app_installations SET suspended_at = :now WHERE installation_id = 201"
            ),
            {"now": datetime.now(UTC)},
        )
    assert (await setup_linked.grant()).status == "changed_try_again"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["snapshot_removed", "installation_removed"])
async def test_github_coverage_loss_never_grants(setup_linked, github_http, change) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        if change == "snapshot_removed":
            await session.execute(
                text(
                    "UPDATE github_app_installations SET github_repo_full_names = "
                    "ARRAY['acme/base'] WHERE installation_id = 201"
                )
            )
        else:
            await session.execute(
                text(
                    "UPDATE github_app_installations SET github_removed_at = :now "
                    "WHERE installation_id = 201"
                ),
                {"now": datetime.now(UTC)},
            )
    assert (await setup_linked.grant()).status == "changed_try_again"


_APP_UNUSABLE_ACME = (
    "Daimon can't use its app on acme.\n"
    "Ask an owner of acme to check the Daimon app in their GitHub settings."
)


@pytest.mark.asyncio
async def test_new_suspended_installation_never_grants(setup_linked, github_http) -> None:
    github_http.installation_suspended = True
    result = await setup_linked.grant()
    assert result.status == "app_unusable"
    assert result.message == _APP_UNUSABLE_ACME


@pytest.mark.asyncio
async def test_failed_refresh_returns_relink(setup_linked, github_http) -> None:
    github_http.refresh_fail = True
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_user_links SET access_expires_at = :past WHERE github_user_id = 300"
            ),
            {"past": datetime.now(UTC) - timedelta(minutes=1)},
        )
    assert (await setup_linked.grant()).status == "relink"


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["manager", "link"])
async def test_revocation_between_lookup_and_commit(
    setup_linked, github_http, monkeypatch, revoked
) -> None:
    async def revoke() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            if revoked == "manager":
                await set_role(session, setup_linked.account.id, Role.USER)
            else:
                await unlink_account(session, account_id=setup_linked.account.id)

    monkeypatch.setattr(linked, "_before_commit", revoke)
    assert (await setup_linked.grant()).status == "changed_try_again"
    async with setup_linked.sessionmaker() as session:
        assert (
            await list_agent_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["removed", "suspended", "snapshot_removed", "installation_removed"]
)
async def test_installation_change_before_commit_retries(
    setup_linked, github_http, monkeypatch, change
) -> None:
    async def change_installation() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            if change == "removed":
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET repo_full_names = "
                        "ARRAY['acme/base'] WHERE installation_id = 201"
                    )
                )
            elif change == "snapshot_removed":
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET github_repo_full_names = "
                        "ARRAY['acme/base'] WHERE installation_id = 201"
                    )
                )
            elif change == "suspended":
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET suspended_at = :now "
                        "WHERE installation_id = 201"
                    ),
                    {"now": datetime.now(UTC)},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET github_removed_at = :now "
                        "WHERE installation_id = 201"
                    ),
                    {"now": datetime.now(UTC)},
                )

    monkeypatch.setattr(linked, "_before_commit", change_installation)
    assert (await setup_linked.grant()).status == "changed_try_again"


@pytest.mark.asyncio
async def test_github_permission_revoked_after_lookup_is_grant_time_authority(
    setup_linked, github_http, monkeypatch
) -> None:
    async def revoke_permission() -> None:
        github_http.permission = "none"

    monkeypatch.setattr(linked, "_before_commit", revoke_permission)
    assert (await setup_linked.grant()).status == "granted"


@pytest.mark.asyncio
async def test_platform_membership_revoked_after_manages_now_returns(
    setup_linked, github_http
) -> None:
    membership = {"active": True}

    async def manages_now(_session: AsyncSession) -> bool:
        allowed = membership["active"]
        membership["active"] = False
        return allowed

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/widgets",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages_now,
    )
    assert result.status == "granted"
    assert membership["active"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["linked", "needs_install", "wrong_person", "expired_pending", "agent_changed"]
)
async def test_pending_grant_resumes_from_personal_link_callback(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    settings = Settings(
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
        hub=HubSettings(
            discord_client_id="discord-client", discord_client_secret=SecretStr("discord-secret")
        ),
    )
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
        settings=settings,
        deployment_default=DeploymentDefault(),
        fernet=setup_linked.fernet,
    )
    auth = AuthIdentity(
        account_id=setup_linked.account.id,
        tenant_id=setup_linked.tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id=setup_linked.tenant.external_id,
        platform_user_id="person",
        is_admin=True,
    )
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="123",
        thread_id="456",
    )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(connect_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    post_link = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_personal_link", post_link)
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets"
    )
    assert result.status == "needs_link"
    post_link.assert_awaited_once()
    url = post_link.await_args.kwargs["url"]
    if scenario == "needs_install":
        github_http.repo_visible = False
    if scenario == "agent_changed":
        monkeypatch.setattr(
            linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=None)
        )
    if scenario == "expired_pending":
        async with setup_linked.sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE github_linked_connect_pending SET expires_at = :past"),
                {"past": datetime.now(UTC) - timedelta(seconds=1)},
            )

    discord_user = "another" if scenario == "wrong_person" else "person"

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            return httpx.Response(200, json={"access_token": "discord-access"})
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": discord_user})
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "github-access",
                    "refresh_token": "github-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 86400,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 300, "login": "person"})
        return github_http.respond(request)

    routes = build_personal_link_routes(
        settings=settings,
        sessionmaker=setup_linked.sessionmaker,
        fernet=setup_linked.fernet,
        runtime=runtime,
        client_factory=lambda: github_http.original_client(transport=httpx.MockTransport(api)),
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/link/platform-callback", routes[1]),
            Route("/oauth/github/link/{token}", routes[0]),
            Route("/oauth/github/callback", routes[2]),
        ]
    )
    async with github_http.original_client(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        opened = await browser.get(url)
        assert opened.status_code == 307
        platform_state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        verified = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "discord-code"},
        )
        if scenario == "wrong_person":
            assert verified.status_code == 403
            assert "<h1>Not your link</h1>" in verified.text
            assert "Ask in Discord for your own link." in verified.text
        else:
            assert verified.status_code == 307
            github_state = parse_qs(urlparse(verified.headers["location"]).query)["state"][0]
            linked_page = await browser.get(
                "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
            )
            assert linked_page.status_code == 200
            assert "close this tab" not in linked_page.text.lower()
            if scenario == "agent_changed":
                assert "<h1>Ask again in chat</h1>" in linked_page.text
                assert "Agent access changed while you were linking." in linked_page.text
            elif scenario == "expired_pending":
                assert "<h1>GitHub linked</h1>" in linked_page.text
                assert "Linked as @person." in _visible(linked_page.text)
            else:
                assert "<h1>GitHub linked</h1>" in linked_page.text
                assert "Linked as @person. Check Discord for the result." in _visible(
                    linked_page.text
                )
                assert "Back to Discord" in linked_page.text
    async with setup_linked.sessionmaker() as session:
        grants = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
    assert len(grants) == (1 if scenario == "linked" else 0)
    assert delivery.await_count == (
        1 if scenario in ("linked", "needs_install", "agent_changed") else 0
    )
    if scenario == "needs_install":
        assert post_link.await_count == 2
        assert post_link.await_args.kwargs["label"] == "Open GitHub setup"
        assert post_link.await_args.kwargs["url"] == (
            "https://github.com/apps/sample-app/installations/new"
        )
        assert "https://" not in delivery.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["needs_install", "needs_link", "relink"])
async def test_pending_resume_sends_private_recovery_button(
    setup_linked, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
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
        fernet=setup_linked.fernet,
    )
    async with setup_linked.sessionmaker.begin() as session:
        await linked_pending.save_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            agent_id=setup_linked.agent_id,
            ma_agent_id="ma_agent",
            agent_name="Agent",
            repo_name="acme/widgets",
            requested_access="write",
            platform="discord",
            platform_user_id="person",
            workspace_id=setup_linked.tenant.external_id,
            parent_channel_id="123",
            thread_id="456",
        )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "requester_manages_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(
        linked_pending,
        "linked_grant",
        AsyncMock(
            return_value=linked.LinkedOutcome(status=status, message="Private setup needed.")
        ),
    )
    delivery = AsyncMock()
    private_button = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    monkeypatch.setattr(connect_tool, "_post_personal_link", private_button)
    result = await linked_pending.resume_pending(
        runtime,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    assert result == ["Private setup needed."]
    delivery.assert_awaited_once()
    assert delivery.await_args.args[2] == "Private setup needed."
    private_button.assert_awaited_once()
    button = private_button.await_args.kwargs
    assert (button["channel_id"], button["thread_id"]) == ("123", "456")
    if status == "needs_install":
        assert button["label"] == "Open GitHub setup"
        assert button["url"] == "https://github.com/apps/sample-app/installations/new"
    else:
        assert button["label"] == "Link GitHub"
        assert button["url"].startswith("https://mcp.test/oauth/github/link/")
    async with setup_linked.sessionmaker.begin() as session:
        pending = await take_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            platform="discord",
            platform_user_id="person",
        )
    assert len(pending) == (0 if status == "needs_install" else 1)
    if pending:
        assert pending[0].requested_access == "write"


@pytest.mark.asyncio
async def test_failed_private_recovery_delivery_still_reports_in_thread(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
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
        fernet=setup_linked.fernet,
    )
    async with setup_linked.sessionmaker.begin() as session:
        await linked_pending.save_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            agent_id=setup_linked.agent_id,
            ma_agent_id="ma_agent",
            agent_name="Agent",
            repo_name="acme/widgets",
            requested_access="write",
            platform="discord",
            platform_user_id="person",
            workspace_id=setup_linked.tenant.external_id,
            parent_channel_id="123",
            thread_id="456",
        )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "requester_manages_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(
        linked_pending,
        "linked_grant",
        AsyncMock(
            return_value=linked.LinkedOutcome(
                status="needs_install", message="Private setup needed."
            )
        ),
    )
    delivery = AsyncMock()
    private_button = AsyncMock(side_effect=RuntimeError("private delivery refused"))
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    monkeypatch.setattr(connect_tool, "_post_personal_link", private_button)
    result = await linked_pending.resume_pending(
        runtime,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    message = "I couldn't send the private GitHub link. Ask me again."
    assert result == [message]
    delivery.assert_awaited_once()
    assert delivery.await_args.args[2] == message


@pytest.mark.asyncio
async def test_each_linked_state_uses_the_agreed_status_and_supporting_line(
    setup_linked, github_http
) -> None:
    """Every result is a status line then a supporting line, with no URL or token."""
    shared = "Everyone who talks to Agent can use this repo through Agent."
    outcomes = []
    github_http.permission = "push"
    outcomes.append(("granted", await setup_linked.grant()))
    github_http.permission = "pull"
    outcomes.append(("granted_read_only", await setup_linked.grant()))
    github_http.permission = "none"
    outcomes.append(("no_github_access", await setup_linked.grant()))
    outcomes.append(("needs_install", await setup_linked.grant(repo_name="acme/new")))
    github_http.permission = "push"
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE github_user_links SET status = 'broken' WHERE github_user_id = 300")
        )
    outcomes.append(("relink", await setup_linked.grant()))
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    outcomes.append(("needs_link", await setup_linked.grant()))
    expected = {
        "granted": f"Agent can now read and change acme/widgets.\n{shared}",
        "granted_read_only": f"Agent can now read acme/widgets.\n{shared}",
        "no_github_access": (
            "Your GitHub account can't open acme/widgets.\n"
            "Ask a server admin who can open this repo on GitHub."
        ),
        "needs_install": (
            "Daimon can't reach acme/new with your GitHub account.\n"
            "If you can open it on GitHub, ask an owner or repo admin to add it to the Daimon app."
        ),
        "relink": "Your GitHub link needs renewing.",
        "needs_link": "Link GitHub to check your repo access.",
    }
    for status, outcome in outcomes:
        assert outcome.status == status
        assert outcome.message == expected[status]
        assert "http" not in outcome.message
        assert "token" not in outcome.message.lower()
    assert linked.CHANGED_MESSAGE == "Access changed while checking.\nAsk again."
    assert (
        linked.INSTALL_PENDING_MESSAGE == "Daimon is refreshing its repo list.\nTry again shortly."
    )
    assert linked.owner_needs_install_message("acme") == (
        "Daimon can't reach acme repos with your GitHub account.\n"
        "If you can open them on GitHub, ask an owner to add them to the Daimon app."
    )
    assert linked.other_installation_message("Agent", "widgets-org") == (
        "Agent already has repos from widgets-org.\n"
        "An agent uses one GitHub account or org at a time. "
        "To switch, remove the widgets-org repos first."
    )
    assert linked.other_installation_message("Agent", "widgets-org", "owner") == (
        "Agent already has repos from widgets-org.\n"
        "An agent uses one GitHub account or org at a time. "
        "To switch, remove all widgets-org repos first."
    )
    for kind in ("repo", "owner"):
        assert linked.other_installation_message("Agent", None, kind) == (
            "Agent already has repos from another GitHub account or org.\n"
            "An agent uses one GitHub account or org at a time. "
            "To switch, remove those repos first."
        )
    assert linked.app_unusable_message("acme") == _APP_UNUSABLE_ACME


@pytest.mark.asyncio
async def test_changed_result_asks_again(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await set_role(session, setup_linked.account.id, Role.USER)
    result = await setup_linked.grant()
    assert result.status == "changed_try_again"
    assert result.message == "Access changed while checking.\nAsk again."


@pytest.mark.asyncio
async def test_private_link_card_puts_the_button_first_on_discord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    class _User:
        async def send(self, **kwargs: object) -> None:
            sent.update(kwargs)

    class _Client:
        async def fetch_user(self, _user_id: int) -> _User:
            return _User()

    class _Rest:
        async def __aenter__(self) -> _Client:
            return _Client()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(connect_tool, "rest_client", lambda _token: _Rest())
    monkeypatch.setattr(connect_tool, "_require_bot_token", lambda _runtime: "token")
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id="workspace",
        platform_user_id="123",
    )
    for label, line in (
        ("Link GitHub", "This link is just for you and works once."),
        ("Open GitHub setup", connect_tool.SETUP_CARD_LINE),
    ):
        sent.clear()
        await connect_tool._post_personal_link(  # pyright: ignore[reportPrivateUsage]
            SimpleNamespace(),  # type: ignore[arg-type]
            auth,
            url="https://private.example/link",
            channel_id="c",
            thread_id="t",
            label=label,
        )
        assert set(sent) == {"view"}, "a layout view carries no separate message content"
        first, second = sent["view"].children  # type: ignore[attr-defined]
        assert [child.label for child in first.children] == [label]
        assert first.children[0].url == "https://private.example/link"
        assert second.content == line


@pytest.mark.asyncio
async def test_private_link_card_puts_the_button_first_on_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(chat_postEphemeral=AsyncMock())
    monkeypatch.setattr(connect_tool, "slack_web_client", AsyncMock(return_value=client))
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="slack",
        external_id="T1",
        platform_user_id="U1",
    )
    await connect_tool._post_personal_link(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(),  # type: ignore[arg-type]
        auth,
        url="https://private.example/link",
        channel_id="c",
        thread_id="t",
    )
    blocks = client.chat_postEphemeral.await_args.kwargs["blocks"]
    assert [block["type"] for block in blocks] == ["actions", "context"]
    assert blocks[0]["elements"][0]["text"]["text"] == "Link GitHub"
    assert blocks[1]["elements"][0]["text"] == "This link is just for you and works once."
    assert "https://" not in client.chat_postEphemeral.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_resume_says_when_agent_access_changed(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SimpleNamespace(client=AsyncMock(), session_factory=setup_linked.sessionmaker)
    async with setup_linked.sessionmaker.begin() as session:
        await linked_pending.save_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            agent_id=setup_linked.agent_id,
            ma_agent_id="ma_agent",
            agent_name="Agent",
            repo_name="acme/widgets",
            platform="discord",
            platform_user_id="person",
            workspace_id=setup_linked.tenant.external_id,
            parent_channel_id="123",
            thread_id="456",
        )
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=None))
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    result = await linked_pending.resume_pending(
        runtime,  # type: ignore[arg-type]
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    assert result == ["Agent access changed while you were linking.\nAsk again in chat."]
    assert delivery.await_args.args[2] == result[0]


@pytest.mark.asyncio
async def test_private_delivery_failure_is_a_plain_message_without_a_button(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
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
        fernet=setup_linked.fernet,
    )
    auth = AuthIdentity(
        account_id=setup_linked.account.id,
        tenant_id=setup_linked.tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id=setup_linked.tenant.external_id,
        platform_user_id="person",
        is_admin=True,
    )
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="123",
        thread_id="456",
    )
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(
        connect_tool,
        "resolve_setup_agent",
        AsyncMock(return_value=SimpleNamespace(id="ma_agent", name="Agent", metadata={})),
    )
    refused = discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "blocked")
    monkeypatch.setattr(connect_tool, "_post_personal_link", AsyncMock(side_effect=refused))
    install = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/new"
    )
    assert install.status == "delivery_failed"
    assert install.message == "I couldn't send the private GitHub setup button. Try again."
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    link = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets"
    )
    assert link.status == "delivery_failed"
    assert link.message == "I couldn't send the private GitHub link. Try again."
    for result in (install, link):
        assert "http" not in result.message


# --- Commit-time authority, pending claims and second installations ---------------


def _runtime(setup_linked) -> McpRuntime:
    return McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
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
        fernet=setup_linked.fernet,
    )


def _admin_auth(setup_linked) -> AuthIdentity:
    return AuthIdentity(
        account_id=setup_linked.account.id,
        tenant_id=setup_linked.tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id=setup_linked.tenant.external_id,
        platform_user_id="person",
        is_admin=True,
    )


def _origin() -> SimpleNamespace:
    return SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="123",
        thread_id="456",
    )


def _patch_tool_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=_origin()))
    monkeypatch.setattr(
        connect_tool,
        "resolve_setup_agent",
        AsyncMock(return_value=SimpleNamespace(id="ma_agent", name="Agent", metadata={})),
    )


async def _save_request(setup_linked, *, thread_id: str, repo_name: str = "acme/widgets") -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await linked_pending.save_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            agent_id=setup_linked.agent_id,
            ma_agent_id="ma_agent",
            agent_name="Agent",
            repo_name=repo_name,
            platform="discord",
            platform_user_id="person",
            workspace_id=setup_linked.tenant.external_id,
            parent_channel_id="123",
            thread_id=thread_id,
        )


async def _count_pending(setup_linked) -> int:
    async with setup_linked.sessionmaker() as session:
        return int(
            await session.scalar(text("SELECT count(*) FROM github_linked_connect_pending")) or 0
        )


async def _grant_count(setup_linked) -> int:
    async with setup_linked.sessionmaker() as session:
        return len(
            await list_agent_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
        )


@pytest_asyncio.fixture
async def demote(setup_linked, db_engine, db_schema):
    """Demote the requester from another connection, so a held-open transaction cannot block it.

    The test engine's pool has two connections and the direct tool holds both.
    """
    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False), db_schema, poolclass=NullPool
    )
    other = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def run() -> None:
        async with other.begin() as session:
            await set_role(session, setup_linked.account.id, Role.USER)

    yield run
    await engine.dispose()


def _locked_manages_now(setup_linked, *, before_read=None):
    """The authority recheck the tools run, optionally racing a demotion before it reads."""

    async def manages_now(session: AsyncSession) -> bool:
        if before_read is not None:
            await before_read()
        account = await get_account(session, setup_linked.account.id, for_share=True)
        if account is None or account.role != Role.ADMIN:
            return False
        return await requester_manages_agent(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            platform="discord",
            platform_user_id="person",
            agent_name="Agent",
            ma_agent_id="ma_agent",
            default=DeploymentDefault(),
            is_daimon_managed=False,
            lock_authority=True,
        )

    return manages_now


async def _linked_grant_with(setup_linked, manages_now, *, repo_name: str = "acme/widgets"):
    return await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name=repo_name,
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages_now,
    )


async def _narrow_owner_installation(setup_linked) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_app_installations SET repo_full_names = ARRAY['acme/widgets'] "
                "WHERE installation_id = 201"
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
async def test_unlocked_role_read_misses_a_demotion_after_the_first_select(
    setup_linked, demote, github_http, repo_name: str
) -> None:
    """Control: a plain read in the SERIALIZABLE transaction still sees the old ADMIN role."""
    await _narrow_owner_installation(setup_linked)

    async def manages_now(session: AsyncSession) -> bool:
        await demote()
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await _linked_grant_with(setup_linked, manages_now, repo_name=repo_name)
    assert result.status in ("granted", "granted_read_only")


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
async def test_locked_role_read_fails_the_grant_when_a_demotion_commits_after_the_snapshot(
    setup_linked, demote, github_http, repo_name: str
) -> None:
    await _narrow_owner_installation(setup_linked)
    manages_now = _locked_manages_now(setup_linked, before_read=demote)
    with pytest.raises(DBAPIError) as raised:
        await _linked_grant_with(setup_linked, manages_now, repo_name=repo_name)
    assert getattr(raised.value.orig, "sqlstate", None) == "40001"
    assert await _grant_count(setup_linked) == 0
    async with setup_linked.sessionmaker() as session:
        assert (
            await list_owner_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            == []
        )
        repos = await list_authorized_repos(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
    assert {r.repo_full_name for r in repos} == {"acme/base"}


@pytest.mark.asyncio
async def test_locked_role_read_waits_for_the_grant_when_the_demotion_comes_later(
    setup_linked, demote, github_http
) -> None:
    """A demotion after the lock blocks until the grant commits: grant first, valid order."""
    demotion: asyncio.Task[None] | None = None

    async def after_lock(session: AsyncSession) -> bool:
        nonlocal demotion
        allowed = await _locked_manages_now(setup_linked)(session)
        demotion = asyncio.create_task(demote())
        await asyncio.sleep(0.3)
        assert not demotion.done()
        return allowed

    result = await _linked_grant_with(setup_linked, after_lock)
    assert result.status == "granted"
    assert demotion is not None
    await demotion
    assert await _grant_count(setup_linked) == 1


@pytest.mark.asyncio
async def test_direct_tool_demotion_after_the_first_select_changes_nothing(
    setup_linked, demote, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tool_agent(monkeypatch)
    real_get_account = connect_tool.get_account
    demoted = False

    async def get_account_after_demotion(session, account_id, **kwargs):
        nonlocal demoted
        if kwargs.get("for_share") and not demoted:
            demoted = True
            await demote()
        return await real_get_account(session, account_id, **kwargs)

    monkeypatch.setattr(connect_tool, "get_account", get_account_after_demotion)
    result = await connect_tool._github_connect_impl(
        _runtime(setup_linked),
        _admin_auth(setup_linked),
        origin_context_id="synthetic",
        repo_name="acme/widgets",
    )
    assert demoted
    assert result.status == "changed_try_again"
    assert result.message == linked.CHANGED_MESSAGE
    assert await _grant_count(setup_linked) == 0


async def _direct_tool_grant(setup_linked):
    return await connect_tool._github_connect_impl(
        _runtime(setup_linked),
        _admin_auth(setup_linked),
        origin_context_id="synthetic",
        repo_name="acme/widgets",
    )


@pytest.mark.asyncio
async def test_direct_tool_grants_when_the_platform_still_shows_an_admin(
    setup_linked, live_admin, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tool_agent(monkeypatch)
    result = await _direct_tool_grant(setup_linked)
    live_admin.assert_awaited_once()
    assert result.status == "granted"
    assert await _grant_count(setup_linked) == 1


@pytest.mark.asyncio
async def test_direct_tool_refuses_a_stored_admin_the_platform_no_longer_shows(
    setup_linked, live_admin, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Teams keeps the stored role when its admin lookup fails, so it can be stale.
    _patch_tool_agent(monkeypatch)
    live_admin.return_value = False
    result = await _direct_tool_grant(setup_linked)
    assert result.status == "ask_admin"
    assert result.message == linked.ask_admin_message("Agent", "acme/widgets")
    assert await _grant_count(setup_linked) == 0


@pytest.mark.asyncio
async def test_direct_tool_with_no_live_admin_answer_grants_nothing_and_says_to_retry(
    setup_linked, live_admin, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tool_agent(monkeypatch)
    live_admin.side_effect = AdminAttestationUnavailable("Teams lookup failed")
    result = await _direct_tool_grant(setup_linked)
    assert result.status == "changed_try_again"
    assert result.message == (
        "Couldn't confirm you're still a server or workspace admin. Try again in a minute."
    )
    assert await _grant_count(setup_linked) == 0


@pytest.mark.asyncio
async def test_direct_tool_owner_grant_also_needs_the_live_admin_read(
    setup_linked, live_admin, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tool_agent(monkeypatch)
    live_admin.return_value = False
    result = await connect_tool._github_connect_impl(
        _runtime(setup_linked),
        _admin_auth(setup_linked),
        origin_context_id="synthetic",
        repo_name="acme/*",
    )
    assert result.status == "ask_admin"
    assert await _grant_count(setup_linked) == 0


@pytest.mark.asyncio
async def test_resume_demotion_after_the_first_select_changes_nothing(
    setup_linked, demote, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    real_get_account = linked_pending.get_account
    locked_reads = 0

    async def get_account_after_demotion(session, account_id, **kwargs):
        nonlocal locked_reads
        if kwargs.get("for_share"):
            locked_reads += 1
            # The first locked read is the precheck outside the grant transaction.
            if locked_reads == 2:
                await demote()
        return await real_get_account(session, account_id, **kwargs)

    monkeypatch.setattr(linked_pending, "get_account", get_account_after_demotion)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked),
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    assert locked_reads == 2
    assert result == [linked.CHANGED_MESSAGE]
    assert delivery.await_args.args[2] == linked.CHANGED_MESSAGE
    assert await _grant_count(setup_linked) == 0
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_resume_grants_when_the_platform_still_shows_an_admin(
    setup_linked, live_admin, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "_deliver_result", AsyncMock())
    result = await linked_pending.resume_pending(
        _runtime(setup_linked),
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    live_admin.assert_awaited_once()
    assert live_admin.await_args.kwargs["requester"].platform_user_id == "person"
    assert result != [] and "Only a server admin" not in result[0]
    assert await _grant_count(setup_linked) == 1
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_resume_refuses_a_requester_demoted_on_the_platform_while_stored_admin(
    setup_linked, live_admin, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2", repo_name="acme/base")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    live_admin.return_value = False
    # The database still says ADMIN: only the live platform read has changed.
    async with setup_linked.sessionmaker() as session:
        stored = await get_account(session, setup_linked.account.id)
    assert stored is not None and stored.role == Role.ADMIN
    result = await linked_pending.resume_pending(
        _runtime(setup_linked),
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    refusals = [linked.ask_admin_message("Agent", name) for name in ("acme/widgets", "acme/base")]
    assert result == refusals
    assert refusals[0].startswith("Only a server admin can give Agent repos.")
    assert [call.args[2] for call in delivery.await_args_list] == refusals
    assert live_admin.await_count == 2
    assert await _grant_count(setup_linked) == 0
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_resume_with_no_live_admin_answer_grants_nothing_and_says_to_retry(
    setup_linked, live_admin, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    live_admin.side_effect = AdminAttestationUnavailable("discord lookup failed")
    result = await linked_pending.resume_pending(
        _runtime(setup_linked),
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    retry = "Couldn't confirm you're still a server or workspace admin. Try again in a minute."
    assert result == [retry]
    assert delivery.await_args.args[2] == retry
    assert await _grant_count(setup_linked) == 0
    assert await _count_pending(setup_linked) == 0


@pytest_asyncio.fixture
async def other_db(db_engine, db_schema):
    """A second connection pool, so a held-open grant transaction cannot block a writer."""
    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False), db_schema, poolclass=NullPool
    )
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", ["agent_lookup", "grant"])
async def test_one_failed_request_does_not_lose_the_rest_of_the_batch(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    if fails == "agent_lookup":
        monkeypatch.setattr(
            linked_pending,
            "find_agent_by_daimon_tag",
            AsyncMock(side_effect=[RuntimeError("agent lookup down"), agent]),
        )
    else:
        monkeypatch.setattr(
            linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent)
        )
        real_linked_grant = linked_pending.linked_grant
        calls = 0

        async def flaky_grant(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("grant exploded")
            return await real_linked_grant(*args, **kwargs)

        monkeypatch.setattr(linked_pending, "linked_grant", flaky_grant)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked),
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    assert result == [
        "Something went wrong.\nAsk again.",
        "Agent can now read and change acme/widgets.\n"
        "Everyone who talks to Agent can use this repo through Agent.",
    ]
    assert [(c.args[1].thread_id, c.args[2]) for c in delivery.await_args_list] == [
        ("t1", result[0]),
        ("t2", result[1]),
    ]
    assert await _grant_count(setup_linked) == 1
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_failure_after_the_grant_keeps_it_resolves_the_row_and_runs_the_next(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock(side_effect=[RuntimeError("thread unavailable"), None])
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked),
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    assert [r.splitlines()[0] for r in result] == [
        "Agent can now read and change acme/widgets."
    ] * 2
    assert delivery.await_count == 2
    assert await _grant_count(setup_linked) == 1
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_a_crash_after_the_claim_leaves_requests_retakeable_after_the_lease(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    scope = {
        "tenant_id": setup_linked.tenant.id,
        "account_id": setup_linked.account.id,
        "platform": "discord",
        "platform_user_id": "person",
    }
    claimed_at = datetime.now(UTC)
    async with setup_linked.sessionmaker.begin() as session:
        taken = await take_pending(session, **scope, now=claimed_at)
    assert [row.thread_id for row in taken] == ["t1", "t2"]
    # The process dies here: nothing resolves the rows.
    assert await _count_pending(setup_linked) == 2
    async with setup_linked.sessionmaker.begin() as session:
        assert await take_pending(session, **scope, now=claimed_at) == []
        assert await take_pending(session, **scope, now=claimed_at + timedelta(seconds=119)) == []
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    runtime = _runtime(setup_linked)
    # Still leased: a second callback finds nothing.
    assert await linked_pending.resume_pending(runtime, **scope) == []
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE github_linked_connect_pending SET claimed_until = :past"),
            {"past": datetime.now(UTC) - timedelta(seconds=1)},
        )
    result = await linked_pending.resume_pending(runtime, **scope)
    assert len(result) == 2
    assert delivery.await_count == 2
    assert await _count_pending(setup_linked) == 0
    assert await _grant_count(setup_linked) == 1


@pytest.mark.asyncio
async def test_expired_requests_are_deleted_not_claimed(setup_linked) -> None:
    await _save_request(setup_linked, thread_id="t1")
    scope = {
        "tenant_id": setup_linked.tenant.id,
        "account_id": setup_linked.account.id,
        "platform": "discord",
        "platform_user_id": "person",
    }
    async with setup_linked.sessionmaker.begin() as session:
        assert (
            await take_pending(session, **scope, now=datetime.now(UTC) + timedelta(hours=1)) == []
        )
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_second_installation_is_a_defined_result(setup_linked, github_http) -> None:
    github_http.second_installation = True
    assert (await setup_linked.grant()).status == "granted"
    result = await setup_linked.grant(repo_name="gadgets/tools")
    assert result.status == "other_installation"
    assert result.message == (
        "Agent already has repos from acme.\nAn agent uses one GitHub account or org at a time. "
        "To switch, remove the acme repos first."
    )
    assert "http" not in result.message
    assert await _grant_count(setup_linked) == 1
    async with setup_linked.sessionmaker() as session:
        assert await get_installation(session, installation_id=202) is None
        repos = await list_authorized_repos(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
    assert 102 not in {r.repo_id for r in repos}


@pytest.mark.asyncio
async def test_second_installation_through_the_public_tool_sends_no_private_card(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_http.second_installation = True
    assert (await setup_linked.grant()).status == "granted"
    _patch_tool_agent(monkeypatch)
    post_link = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_personal_link", post_link)
    result = await connect_tool._github_connect_impl(
        _runtime(setup_linked),
        _admin_auth(setup_linked),
        origin_context_id="synthetic",
        repo_name="gadgets/tools",
    )
    assert result.status == "other_installation"
    assert result.message == (
        "Agent already has repos from acme.\nAn agent uses one GitHub account or org at a time. "
        "To switch, remove the acme repos first."
    )
    post_link.assert_not_awaited()
    assert await _grant_count(setup_linked) == 1


@pytest.mark.asyncio
async def test_second_installation_on_resume_posts_to_the_thread_and_runs_the_next(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_http.second_installation = True
    assert (await setup_linked.grant()).status == "granted"
    await _save_request(setup_linked, thread_id="t1", repo_name="gadgets/tools")
    await _save_request(setup_linked, thread_id="t2")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked),
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    assert result[0] == (
        "Agent already has repos from acme.\nAn agent uses one GitHub account or org at a time. "
        "To switch, remove the acme repos first."
    )
    assert result[1].startswith("Agent can now read and change acme/widgets.")
    assert [(c.args[1].thread_id, c.args[2]) for c in delivery.await_args_list] == [
        ("t1", result[0]),
        ("t2", result[1]),
    ]
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_coverage_removed_while_the_repo_permission_get_is_outstanding(
    setup_linked, github_http
) -> None:
    """Discovery sees the repo, then coverage goes: nothing is granted or restored."""

    async def remove_coverage() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            await session.execute(
                text(
                    "UPDATE github_app_installations SET repo_full_names = ARRAY['acme/base'] "
                    "WHERE installation_id = 201"
                )
            )

    github_http.during_repo_get = remove_coverage
    result = await setup_linked.grant()
    assert result.status == "changed_try_again"
    assert await _grant_count(setup_linked) == 0
    async with setup_linked.sessionmaker() as session:
        installation = await get_installation(session, installation_id=201)
        repos = await list_authorized_repos(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
    assert installation is not None
    assert list(installation.repo_full_names) == ["acme/base"]
    assert {r.repo_full_name for r in repos} == {"acme/base"}


async def _seed_second_installation(setup_linked) -> None:
    """A second org's installation the agent could be pointed at."""
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "INSERT INTO github_app_installations "
                "(installation_id, account_login, app, account_id, account_type, "
                "repository_selection, repo_full_names) VALUES "
                "(202, 'gadgets', 'github_app', 13, 'Organization', 'selected', "
                "ARRAY['gadgets/tools'])"
            )
        )


@pytest.fixture
def two_orgs(setup_linked, github_http, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Two orgs, each fully visible to the person, for owner grants on either."""
    github_http.second_installation = True
    original = github_http.original_client

    def respond(request: httpx.Request) -> httpx.Response:
        response = github_http.respond(request)
        if request.url.path == "/user/installations/201/repositories":
            body = response.json()
            body["repositories"].append(
                {
                    "id": 100,
                    "full_name": "acme/base",
                    "owner": {"id": 12, "login": "acme"},
                    "permissions": body["repositories"][0]["permissions"],
                }
            )
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr(
        linked.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond)),
    )
    return github_http


async def _owner_installations(setup_linked) -> list[int]:
    async with setup_linked.sessionmaker() as session:
        return sorted(
            grant.installation_id
            for grant in await list_owner_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
        )


_OTHER_ORG_MESSAGE = (
    "Agent already has repos from {org}.\nAn agent uses one GitHub account or org at a time. "
    "To switch, remove {scope} first."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "org", "scope"),
    [
        # An owner grant blocks: every repo of the org has to go, not one by one.
        ("acme/*", "gadgets/tools", "acme", "all acme repos"),
        ("acme/widgets", "gadgets/*", "acme", "the acme repos"),
        ("acme/*", "gadgets/*", "acme", "all acme repos"),
        ("gadgets/*", "acme/widgets", "gadgets", "all gadgets repos"),
    ],
)
async def test_owner_grants_keep_one_installation_per_agent(
    setup_linked, two_orgs, first: str, second: str, org: str, scope: str
) -> None:
    await _seed_second_installation(setup_linked)
    assert (await setup_linked.grant(repo_name=first)).status.startswith("granted")
    explicit_before = await _grant_count(setup_linked)
    owners_before = await _owner_installations(setup_linked)
    result = await setup_linked.grant(repo_name=second)
    assert result.status == "other_installation"
    assert result.message == _OTHER_ORG_MESSAGE.format(org=org, scope=scope)
    assert await _grant_count(setup_linked) == explicit_before
    assert await _owner_installations(setup_linked) == owners_before


@pytest.mark.asyncio
async def test_owner_grant_for_the_same_installation_stays_idempotent(
    setup_linked, two_orgs
) -> None:
    assert (await setup_linked.grant(repo_name="acme/*")).status.startswith("granted")
    assert (await setup_linked.grant(repo_name="acme/*")).status.startswith("granted")
    assert (await setup_linked.grant(repo_name="acme/widgets")).status == "granted"
    assert (await setup_linked.grant(repo_name="acme/*")).status.startswith("granted")
    assert await _owner_installations(setup_linked) == [201]
    assert await _grant_count(setup_linked) == 1


@pytest.mark.asyncio
async def test_removing_every_grant_frees_the_agent_for_another_org(setup_linked, two_orgs) -> None:
    await _seed_second_installation(setup_linked)
    assert (await setup_linked.grant(repo_name="acme/*")).status.startswith("granted")
    assert (await setup_linked.grant(repo_name="acme/widgets")).status == "granted"
    assert (await setup_linked.grant(repo_name="gadgets/*")).status == "other_installation"
    async with setup_linked.sessionmaker.begin() as session:
        for table in ("agent_github_grants", "agent_github_owner_grants"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t AND agent_id = :a"),
                {"t": setup_linked.tenant.id, "a": setup_linked.agent_id},
            )
    assert (await setup_linked.grant(repo_name="gadgets/*")).status.startswith("granted")
    assert await _owner_installations(setup_linked) == [202]


def _pending_scope(setup_linked) -> dict:
    return {
        "tenant_id": setup_linked.tenant.id,
        "account_id": setup_linked.account.id,
        "platform": "discord",
        "platform_user_id": "person",
    }


async def _take_at(setup_linked, now: datetime | None = None, **overrides):
    async with setup_linked.sessionmaker.begin() as session:
        return await take_pending(session, **{**_pending_scope(setup_linked), **overrides}, now=now)


@pytest.mark.asyncio
async def test_resolve_with_another_claims_token_deletes_nothing(setup_linked) -> None:
    await _save_request(setup_linked, thread_id="t1")
    (first,) = await _take_at(setup_linked)
    # Its lease lapsed, so a second callback takes the same request under a new token.
    (second,) = await _take_at(setup_linked, datetime.now(UTC) + CLAIM_LEASE + timedelta(seconds=1))
    assert first.id == second.id
    assert first.claim_token != second.claim_token
    async with setup_linked.sessionmaker.begin() as session:
        assert not await resolve_pending(session, first.id, first.claim_token)
        assert not await resolve_pending(session, first.id, None)
        assert not await renew(session, first.id, first.claim_token)
    assert await _count_pending(setup_linked) == 1
    async with setup_linked.sessionmaker.begin() as session:
        assert await renew(session, second.id, second.claim_token)
        assert await resolve_pending(session, second.id, second.claim_token)
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_a_callback_that_lost_its_claim_skips_the_request_without_delivering(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "requester_manages_agent", AsyncMock(return_value=True))
    grant = AsyncMock(return_value=linked.LinkedOutcome(status="granted", message="Done."))
    monkeypatch.setattr(linked_pending, "linked_grant", grant)
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    real_take = linked_pending._take_pending  # pyright: ignore[reportPrivateUsage]
    stolen: list = []

    async def take_then_lose_the_lease(runtime, **kwargs):
        rows = await real_take(runtime, **kwargs)
        async with setup_linked.sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE github_linked_connect_pending SET claimed_until = :past"),
                {"past": datetime.now(UTC) - timedelta(seconds=1)},
            )
        stolen.extend(await real_take(runtime, **kwargs))
        return rows

    monkeypatch.setattr(linked_pending, "_take_pending", take_then_lose_the_lease)
    kwargs = {**_pending_scope(setup_linked)}
    assert await linked_pending.resume_pending(_runtime(setup_linked), **kwargs) == []
    delivery.assert_not_awaited()
    grant.assert_not_awaited()
    assert await _count_pending(setup_linked) == 1
    # The callback that owns it answers once.
    monkeypatch.setattr(linked_pending, "_take_pending", real_take)
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE github_linked_connect_pending SET claimed_until = :past"),
            {"past": datetime.now(UTC) - timedelta(seconds=1)},
        )
    assert await linked_pending.resume_pending(_runtime(setup_linked), **kwargs) == ["Done."]
    assert delivery.await_count == 1
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_expiry_cleanup_keeps_a_claimed_request_until_its_lease_is_long_past(
    setup_linked,
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    rows = await _take_at(setup_linked)
    assert len(rows) == 2
    now = datetime.now(UTC)
    async with setup_linked.sessionmaker.begin() as session:
        # Both passed their expiry. t1's holder crashed long ago; t2's holder is
        # slow and its lease lapsed a moment ago, but it may still answer t2.
        await session.execute(
            text(
                "UPDATE github_linked_connect_pending SET expires_at = :past, "
                "claimed_until = CASE WHEN thread_id = 't1' THEN CAST(:long_ago AS timestamptz) "
                "ELSE CAST(:past AS timestamptz) END"
            ),
            {
                "past": now - timedelta(seconds=1),
                "long_ago": now - CLAIMED_GARBAGE_AFTER - timedelta(seconds=1),
            },
        )
    # Another person's callback runs the cleanup.
    async with setup_linked.sessionmaker.begin() as session:
        assert not await take_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=uuid.uuid4(),
            platform="discord",
            platform_user_id="someone-else",
            now=now,
        )
    async with setup_linked.sessionmaker() as session:
        threads = list(
            await session.scalars(text("SELECT thread_id FROM github_linked_connect_pending"))
        )
    assert threads == ["t2"]
    # The slow holder still resolves t2 with its own token.
    t2 = next(row for row in rows if row.thread_id == "t2")
    async with setup_linked.sessionmaker.begin() as session:
        assert await resolve_pending(session, t2.id, t2.claim_token)
    assert await _count_pending(setup_linked) == 0


async def _recovery_fixture(setup_linked, monkeypatch: pytest.MonkeyPatch):
    await _save_request(setup_linked, thread_id="t1")
    (row,) = await _take_at(setup_linked)
    post = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_personal_link", post)
    return row, post


@pytest.mark.asyncio
async def test_recovery_replaces_the_original_request_in_one_transaction(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, post = await _recovery_fixture(setup_linked, monkeypatch)
    await linked_pending._deliver_recovery(  # pyright: ignore[reportPrivateUsage]
        _runtime(setup_linked), row, status="needs_link"
    )
    post.assert_awaited_once()
    async with setup_linked.sessionmaker() as session:
        ids = list(await session.scalars(text("SELECT id FROM github_linked_connect_pending")))
    assert len(ids) == 1
    assert ids[0] != row.id


@pytest.mark.asyncio
async def test_a_failure_between_saving_the_replacement_and_resolving_leaves_neither(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, post = await _recovery_fixture(setup_linked, monkeypatch)
    monkeypatch.setattr(linked_pending, "resolve", AsyncMock(side_effect=RuntimeError("down")))
    with pytest.raises(RuntimeError):
        await linked_pending._deliver_recovery(  # pyright: ignore[reportPrivateUsage]
            _runtime(setup_linked), row, status="needs_link"
        )
    post.assert_not_awaited()
    async with setup_linked.sessionmaker() as session:
        ids = list(await session.scalars(text("SELECT id FROM github_linked_connect_pending")))
    assert ids == [row.id]


@pytest.mark.asyncio
async def test_recovery_by_a_callback_that_lost_the_claim_adds_nothing(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, post = await _recovery_fixture(setup_linked, monkeypatch)
    await _take_at(setup_linked, datetime.now(UTC) + CLAIM_LEASE + timedelta(seconds=1))
    with pytest.raises(ClaimLostError):
        await linked_pending._deliver_recovery(  # pyright: ignore[reportPrivateUsage]
            _runtime(setup_linked), row, status="needs_link"
        )
    post.assert_not_awaited()
    async with setup_linked.sessionmaker() as session:
        ids = list(await session.scalars(text("SELECT id FROM github_linked_connect_pending")))
    assert ids == [row.id]


@pytest.mark.asyncio
async def test_consecutive_read_grants_through_the_public_tool_stay_read_only(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_http.permission = "pull"
    _patch_tool_agent(monkeypatch)
    monkeypatch.setattr(connect_tool, "_post_personal_link", AsyncMock())
    for _ in range(2):
        result = await connect_tool._github_connect_impl(
            _runtime(setup_linked),
            _admin_auth(setup_linked),
            origin_context_id="synthetic",
            repo_name="acme/widgets",
        )
        assert result.status == "granted_read_only"
        assert result.message == linked.granted_message("Agent", "acme/widgets", "read")
        assert "change" not in result.message
        async with setup_linked.sessionmaker() as session:
            grants = await list_agent_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
        assert [(g.repo_id, g.baseline_access, g.ceiling_access) for g in grants] == [
            (101, "read", "read")
        ]


# --- pending timing: heartbeat and per-request deadline -----------------------


async def _shift_pending_back(setup_linked, delta: timedelta) -> None:
    """Move every stored pending timestamp into the past, as if `delta` had gone by."""
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_linked_connect_pending "
                "SET expires_at = expires_at - CAST(:delta AS interval), "
                "claimed_until = claimed_until - CAST(:delta AS interval)"
            ),
            {"delta": delta},
        )


async def _pending_threads(setup_linked) -> list[str]:
    async with setup_linked.sessionmaker() as session:
        return sorted(
            await session.scalars(text("SELECT thread_id FROM github_linked_connect_pending"))
        )


@pytest.mark.asyncio
async def test_a_slow_first_request_keeps_the_second_requests_lease_alive(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    stranger = {
        "tenant_id": setup_linked.tenant.id,
        "account_id": uuid.uuid4(),
        "platform": "discord",
        "platform_user_id": "someone-else",
    }
    survived: list[list[str]] = []

    async def outcome(runtime, row, **_kwargs) -> str:
        if row.thread_id == "t1":
            # The first request runs for more than the garbage window: every stored
            # timestamp, the second row's included, is now older than that.
            await _shift_pending_back(
                setup_linked, CLAIMED_GARBAGE_AFTER + CLAIM_LEASE + timedelta(minutes=5)
            )
            return "first"
        # Another person's callback cleans up while the second request runs.
        async with setup_linked.sessionmaker.begin() as session:
            await take_pending(session, **stranger)
        survived.append(await _pending_threads(setup_linked))
        return "second"

    monkeypatch.setattr(linked_pending, "_outcome_message", outcome)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked), **_pending_scope(setup_linked)
    )
    assert result == ["first", "second"]
    assert survived == [["t2"]], "the heartbeat renewed the second row before it ran"
    assert [(c.args[1].thread_id, c.args[2]) for c in delivery.await_args_list] == [
        ("t1", "first"),
        ("t2", "second"),
    ]
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_without_the_heartbeat_the_stranger_cleanup_would_delete_the_second_row(
    setup_linked,
) -> None:
    """Control: the same elapsed time with no renewal loses the row."""
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    await _take_at(setup_linked)
    await _shift_pending_back(
        setup_linked, CLAIMED_GARBAGE_AFTER + CLAIM_LEASE + timedelta(minutes=5)
    )
    async with setup_linked.sessionmaker.begin() as session:
        await take_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=uuid.uuid4(),
            platform="discord",
            platform_user_id="someone-else",
        )
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_a_row_another_callback_took_is_skipped_even_when_others_are_held(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)

    async def outcome(runtime, row, **_kwargs) -> str:
        if row.thread_id == "t1":
            # t2's lease lapsed meanwhile and a second callback took it.
            async with setup_linked.sessionmaker.begin() as session:
                await session.execute(
                    text(
                        "UPDATE github_linked_connect_pending SET claim_token = :other "
                        "WHERE thread_id = 't2'"
                    ),
                    {"other": uuid.uuid4()},
                )
        return f"answered {row.thread_id}"

    monkeypatch.setattr(linked_pending, "_outcome_message", outcome)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked), **_pending_scope(setup_linked)
    )
    assert result == ["answered t1"]
    assert [c.args[1].thread_id for c in delivery.await_args_list] == ["t1"]
    assert await _pending_threads(setup_linked) == ["t2"]


@pytest.mark.asyncio
async def test_a_request_past_its_deadline_gets_the_failure_line_and_the_next_still_runs(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    monkeypatch.setattr(linked_pending, "PER_REQUEST_DEADLINE", timedelta(milliseconds=100))
    cancelled: list[str] = []

    async def outcome(runtime, row, **_kwargs) -> str:
        if row.thread_id == "t1":
            try:
                await asyncio.sleep(30)  # GitHub listing that never ends
            except asyncio.CancelledError:
                cancelled.append(row.thread_id)
                raise
        return "second"

    monkeypatch.setattr(linked_pending, "_outcome_message", outcome)
    result = await asyncio.wait_for(
        linked_pending.resume_pending(_runtime(setup_linked), **_pending_scope(setup_linked)),
        timeout=10,
    )
    assert result == [linked_pending.FAILED_MESSAGE, "second"]
    assert cancelled == ["t1"]
    assert [(c.args[1].thread_id, c.args[2]) for c in delivery.await_args_list] == [
        ("t1", linked_pending.FAILED_MESSAGE),
        ("t2", "second"),
    ]
    assert await _count_pending(setup_linked) == 0


@pytest.mark.asyncio
async def test_the_failure_line_after_a_deadline_is_best_effort(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    monkeypatch.setattr(
        linked_pending, "_deliver_result", AsyncMock(side_effect=RuntimeError("thread gone"))
    )
    monkeypatch.setattr(linked_pending, "PER_REQUEST_DEADLINE", timedelta(milliseconds=50))

    async def outcome(runtime, row, **_kwargs) -> str:
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(linked_pending, "_outcome_message", outcome)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked), **_pending_scope(setup_linked)
    )
    assert result == [linked_pending.FAILED_MESSAGE]
    assert await _count_pending(setup_linked) == 0


def test_the_lease_and_deadline_fit_inside_the_garbage_window() -> None:
    from daimon.core.stores import github_linked_pending as store

    gap = (
        store.PER_REQUEST_DEADLINE
        + store.DETACHED_REFRESH_SETTLE
        + store.FAILURE_DELIVERY_DEADLINE
        + 2 * store.DB_STATEMENT_DEADLINE
    )
    assert gap == timedelta(seconds=145)
    assert gap == store.RENEWAL_GAP_BOUND
    assert gap + store.CANCEL_CLEANUP_ALLOWANCE + store.CLAIM_LEASE < store.CLAIMED_GARBAGE_AFTER


def test_the_gap_bound_counts_the_settle_window_a_cancelled_request_waits() -> None:
    from daimon.core import github_requester_access
    from daimon.core.stores import github_linked_pending as store

    assert (
        store.DETACHED_REFRESH_SETTLE.total_seconds()
    ) == github_requester_access.DETACHED_REFRESH_SETTLE_S


@pytest.mark.asyncio
async def test_a_statement_timeout_on_the_heartbeat_fails_every_remaining_request_and_stops(
    setup_linked, other_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real lock wait past `lock_timeout` fails the heartbeat: every held request hears it."""
    from daimon.core.stores import github_linked_pending as store

    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    monkeypatch.setattr(store, "DB_STATEMENT_DEADLINE", timedelta(milliseconds=200))
    monkeypatch.setattr(linked_pending, "DB_STATEMENT_DEADLINE", timedelta(milliseconds=200))
    holder = other_db.begin()
    held: list[AsyncSession] = []
    real_take = linked_pending._take_pending

    async def take_then_lock(*args, **kwargs):
        rows = await real_take(*args, **kwargs)
        # Another transaction now holds the first row: the heartbeat's UPDATE waits on it.
        held.append(await holder.__aenter__())
        await held[0].execute(
            text("SELECT 1 FROM github_linked_connect_pending WHERE thread_id = 't1' FOR UPDATE")
        )
        return rows

    async def deliver(runtime, row, message) -> None:
        if holder_open:
            holder_open.clear()
            await holder.__aexit__(None, None, None)

    holder_open = [True]
    delivery = AsyncMock(side_effect=deliver)
    monkeypatch.setattr(linked_pending, "_take_pending", take_then_lock)
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)

    async def outcome(runtime, row, **_kwargs) -> str:
        return f"answered {row.thread_id}"

    monkeypatch.setattr(linked_pending, "_outcome_message", outcome)
    try:
        result = await asyncio.wait_for(
            linked_pending.resume_pending(_runtime(setup_linked), **_pending_scope(setup_linked)),
            timeout=15,
        )
    finally:
        if holder_open:
            await holder.__aexit__(None, None, None)
    assert result == [linked_pending.FAILED_MESSAGE, linked_pending.FAILED_MESSAGE]
    assert [(c.args[1].thread_id, c.args[2]) for c in delivery.await_args_list] == [
        ("t1", linked_pending.FAILED_MESSAGE),
        ("t2", linked_pending.FAILED_MESSAGE),
    ]
    # Neither request is resolved (the database was not answering): both stay
    # claimed until their lease lapses, then a later take runs them again.
    assert await _pending_threads(setup_linked) == ["t1", "t2"]


@pytest.mark.asyncio
async def test_a_resolve_that_times_out_does_not_stop_the_batch(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    await _save_request(setup_linked, thread_id="t2")
    monkeypatch.setattr(linked_pending, "_deliver_result", AsyncMock())
    real_resolve = linked_pending._resolve_pending
    calls: list[str] = []

    async def resolve(runtime, row) -> bool:
        calls.append(row.thread_id)
        if row.thread_id == "t1":
            raise TimeoutError
        return await real_resolve(runtime, row)

    async def outcome(runtime, row, **_kwargs) -> str:
        return f"answered {row.thread_id}"

    monkeypatch.setattr(linked_pending, "_resolve_pending", resolve)
    monkeypatch.setattr(linked_pending, "_outcome_message", outcome)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked), **_pending_scope(setup_linked)
    )
    assert result == ["answered t1", "answered t2"]
    assert calls == ["t1", "t2"]
    assert await _pending_threads(setup_linked) == ["t1"]


# --- app_unusable: the app is installed but cannot be used ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
async def test_an_unusable_app_sends_no_setup_card_through_the_public_tool(
    setup_linked, two_orgs, monkeypatch: pytest.MonkeyPatch, repo_name: str
) -> None:
    two_orgs.installation_suspended = True
    _patch_tool_agent(monkeypatch)
    post_link = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_personal_link", post_link)
    result = await connect_tool._github_connect_impl(
        _runtime(setup_linked),
        _admin_auth(setup_linked),
        origin_context_id="synthetic",
        repo_name=repo_name,
    )
    assert result.status == "app_unusable"
    assert result.message == _APP_UNUSABLE_ACME
    post_link.assert_not_awaited()
    assert await _grant_count(setup_linked) == 0
    assert await _owner_installations(setup_linked) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
async def test_an_unusable_app_is_reported_by_the_grant_on_both_paths(
    setup_linked, two_orgs, repo_name: str
) -> None:
    two_orgs.installation_suspended = True
    result = await setup_linked.grant(repo_name=repo_name)
    assert (result.status, result.message) == ("app_unusable", _APP_UNUSABLE_ACME)


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
@pytest.mark.parametrize("status", [403, 404])
async def test_a_refused_installation_lookup_is_an_unusable_app(
    setup_linked, two_orgs, repo_name: str, status: int
) -> None:
    two_orgs.installation_detail_status = status
    result = await setup_linked.grant(repo_name=repo_name)
    assert (result.status, result.message) == ("app_unusable", _APP_UNUSABLE_ACME)
    assert await _grant_count(setup_linked) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
@pytest.mark.parametrize("status", [401, 500, 502])
async def test_an_installation_lookup_that_fails_is_github_not_answering_never_a_relink(
    setup_linked, two_orgs, repo_name: str, status: int
) -> None:
    """A 401 on the App's own JWT is Daimon's credential, not the person's link."""
    two_orgs.installation_detail_status = status
    with structlog.testing.capture_logs() as logs:
        result = await setup_linked.grant(repo_name=repo_name)
    assert (result.status, result.message) == (
        "changed_try_again",
        "GitHub didn't answer.\nAsk again in chat.",
    )
    assert result.status != "relink" and linked.RELINK_MESSAGE not in result.message
    assert await _grant_count(setup_linked) == 0
    [failure] = [
        entry
        for entry in logs
        if entry["event"] == "github.linked_grant.installation_detail_failed"
    ]
    assert failure["status_code"] == status
    assert failure["app_credential_rejected"] is (status == 401)


@pytest.mark.asyncio
async def test_a_rate_limited_installation_lookup_is_not_an_unusable_app(
    setup_linked, two_orgs, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def limited(*_args: object, **_kwargs: object) -> None:
        request = httpx.Request("GET", "https://api.github.com/app/installations/201")
        raise httpx.HTTPStatusError(
            "limited",
            request=request,
            response=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, request=request),
        )

    monkeypatch.setattr(linked, "get_app_installation_details", limited)
    result = await setup_linked.grant(repo_name="acme/widgets")
    assert result.status == "changed_try_again"
    assert result.message == "GitHub didn't answer.\nAsk again in chat."


@pytest.mark.asyncio
async def test_resume_sends_no_private_card_for_an_unusable_app(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_request(setup_linked, thread_id="t1")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "requester_manages_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(
        linked_pending,
        "linked_grant",
        AsyncMock(
            return_value=linked.LinkedOutcome(
                status="app_unusable", message=linked.app_unusable_message("acme")
            )
        ),
    )
    delivery = AsyncMock()
    private_button = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    monkeypatch.setattr(connect_tool, "_post_personal_link", private_button)
    result = await linked_pending.resume_pending(
        _runtime(setup_linked), **_pending_scope(setup_linked)
    )
    assert result == [_APP_UNUSABLE_ACME]
    delivery.assert_awaited_once()
    private_button.assert_not_awaited()
    assert await _count_pending(setup_linked) == 0


# --- one installation under concurrency ----------------------------------------


async def _seed_gadgets_server_wide_repo(setup_linked) -> None:
    """A second org's installation with a server-wide repo, as the fast path grants."""
    await _seed_second_installation(setup_linked)
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos "
                "(tenant_id, repo_id, scope_agent_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id, "
                "authorized_by_account_id, status) VALUES "
                "(:tenant_id, 102, NULL, 13, 202, 'gadgets/tools', 'read', 300, "
                ":account_id, 'active')"
            ),
            {"tenant_id": setup_linked.tenant.id, "account_id": setup_linked.account.id},
        )


async def _fast_path_explicit_grant(session: AsyncSession, setup_linked) -> None:
    """What the server-wide fast path does: `stage_grant` in a READ COMMITTED transaction."""
    await stage_grant(
        session,
        tenant_id=setup_linked.tenant.id,
        agent_id=setup_linked.agent_id,
        repo_id=102,
        baseline_access="read",
        ceiling_access="read",
        granted_by_account_id=setup_linked.account.id,
        agent_name="Agent",
    )


async def _owner_a_through_the_tool(setup_linked, monkeypatch, race):
    """Owner-A through the public tool; `race` runs after its SERIALIZABLE snapshot exists."""
    _patch_tool_agent(monkeypatch)
    monkeypatch.setattr(connect_tool, "_post_personal_link", AsyncMock())
    real = connect_tool.requester_manages_agent
    raced = False

    async def racing(session, **kwargs):
        nonlocal raced
        if kwargs.get("lock_authority") and not raced:
            raced = True
            await race()
        return await real(session, **kwargs)

    monkeypatch.setattr(connect_tool, "requester_manages_agent", racing)
    return await connect_tool._github_connect_impl(
        _runtime(setup_linked),
        _admin_auth(setup_linked),
        origin_context_id="synthetic",
        repo_name="acme/*",
    )


@pytest.mark.asyncio
async def test_fast_path_explicit_grant_committed_after_the_owner_snapshot_fails_the_owner_grant(
    setup_linked, two_orgs, other_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_gadgets_server_wide_repo(setup_linked)

    async def explicit_b_commits() -> None:
        async with other_db.begin() as session:
            await _fast_path_explicit_grant(session, setup_linked)

    result = await _owner_a_through_the_tool(setup_linked, monkeypatch, explicit_b_commits)
    assert result.status == "changed_try_again"
    assert result.message == linked.CHANGED_MESSAGE
    assert await _grant_count(setup_linked) == 1, "explicit-B committed"
    assert await _owner_installations(setup_linked) == [], "owner-A rolled back"
    # Asking again sees B and refuses cleanly.
    again = await setup_linked.grant(repo_name="acme/*")
    assert again.status == "other_installation"
    assert again.message == _OTHER_ORG_MESSAGE.format(org="gadgets", scope="the gadgets repos")


@pytest.mark.asyncio
async def test_the_old_advisory_lock_let_both_installations_through(
    setup_linked, two_orgs, other_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the same schedule with the lock this replaced ends on two installations."""
    await _seed_gadgets_server_wide_repo(setup_linked)

    async def advisory_lock(session, *, tenant_id, agent_id) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"github-agent-grants:{tenant_id}:{agent_id}"},
        )

    monkeypatch.setattr(github_access, "lock_agent_grants", advisory_lock)

    async def explicit_b_commits() -> None:
        async with other_db.begin() as session:
            await _fast_path_explicit_grant(session, setup_linked)

    result = await _owner_a_through_the_tool(setup_linked, monkeypatch, explicit_b_commits)
    assert result.status.startswith("granted")
    assert await _grant_count(setup_linked) == 1
    assert await _owner_installations(setup_linked) == [201], "two installations: the bug"


@pytest.mark.asyncio
async def test_owner_grant_waiting_on_a_fast_path_grant_fails_when_that_commits(
    setup_linked, two_orgs, other_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse order: explicit-B holds the fence first and owner-A queues behind it."""
    await _seed_gadgets_server_wide_repo(setup_linked)
    b_ready = asyncio.Event()
    b_commit = asyncio.Event()

    async def explicit_b() -> None:
        async with other_db.begin() as session:
            await _fast_path_explicit_grant(session, setup_linked)
            b_ready.set()
            await b_commit.wait()

    b_task = asyncio.create_task(explicit_b())
    await asyncio.wait_for(b_ready.wait(), timeout=10)

    async def noop() -> None:
        return None

    owner_a = asyncio.create_task(_owner_a_through_the_tool(setup_linked, monkeypatch, noop))
    await asyncio.sleep(0.5)
    assert not owner_a.done(), "owner-A waits for the fence B holds"
    b_commit.set()
    await b_task
    result = await asyncio.wait_for(owner_a, timeout=10)
    assert result.status == "changed_try_again"
    assert await _grant_count(setup_linked) == 1
    assert await _owner_installations(setup_linked) == []


@pytest.mark.asyncio
async def test_fast_path_grant_waiting_on_an_owner_grant_sees_it_and_refuses(
    setup_linked, two_orgs, other_db
) -> None:
    """READ COMMITTED explicit-B queues behind owner-A, then reads fresh state."""
    await _seed_gadgets_server_wide_repo(setup_linked)
    a_ready = asyncio.Event()
    a_commit = asyncio.Event()

    async def owner_a() -> None:
        async with other_db.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            await require_owner_grant_installations(
                session,
                tenant_id=setup_linked.tenant.id,
                agent_id=setup_linked.agent_id,
                installation_ids={201},
                agent_name="Agent",
            )
            await upsert_owner_grant(
                session,
                tenant_id=setup_linked.tenant.id,
                agent_id=setup_linked.agent_id,
                installation_id=201,
                owner_login="acme",
                access="read",
                account_id=setup_linked.account.id,
            )
            a_ready.set()
            await a_commit.wait()

    a_task = asyncio.create_task(owner_a())
    await asyncio.wait_for(a_ready.wait(), timeout=10)

    async def explicit_b() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            await _fast_path_explicit_grant(session, setup_linked)

    b_task = asyncio.create_task(explicit_b())
    await asyncio.sleep(0.5)
    assert not b_task.done(), "explicit-B waits for the fence owner-A holds"
    a_commit.set()
    await a_task
    with pytest.raises(SecondInstallationError) as refused:
        await asyncio.wait_for(b_task, timeout=10)
    assert (refused.value.org, refused.value.kind) == ("acme", "owner")
    assert await _grant_count(setup_linked) == 0
    assert await _owner_installations(setup_linked) == [201]


@pytest.mark.asyncio
async def test_both_paths_bump_the_same_fence_row(setup_linked, two_orgs) -> None:
    assert (await setup_linked.grant(repo_name="acme/widgets")).status == "granted"
    assert (await setup_linked.grant(repo_name="acme/*")).status.startswith("granted")
    async with setup_linked.sessionmaker() as session:
        version = await session.scalar(
            text(
                "SELECT version FROM agent_github_grant_fences "
                "WHERE tenant_id = :t AND agent_id = :a"
            ),
            {"t": setup_linked.tenant.id, "a": setup_linked.agent_id},
        )
    assert version is not None and version >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["40001", "40P01"])
async def test_a_deadlock_or_serialization_failure_asks_again_in_the_tool_and_on_resume(
    setup_linked, monkeypatch: pytest.MonkeyPatch, sqlstate: str
) -> None:
    failure = DBAPIError("grant", {}, SimpleNamespace(sqlstate=sqlstate))
    grant = AsyncMock(side_effect=failure)
    _patch_tool_agent(monkeypatch)
    monkeypatch.setattr(connect_tool, "linked_grant", grant)
    monkeypatch.setattr(connect_tool, "_post_personal_link", AsyncMock())
    result = await connect_tool._github_connect_impl(
        _runtime(setup_linked),
        _admin_auth(setup_linked),
        origin_context_id="synthetic",
        repo_name="acme/widgets",
    )
    assert (result.status, result.message) == ("changed_try_again", linked.CHANGED_MESSAGE)

    await _save_request(setup_linked, thread_id="t1")
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "requester_manages_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(linked_pending, "linked_grant", grant)
    monkeypatch.setattr(linked_pending, "_deliver_result", AsyncMock())
    resumed = await linked_pending.resume_pending(
        _runtime(setup_linked), **_pending_scope(setup_linked)
    )
    assert resumed == [linked.CHANGED_MESSAGE]


@pytest.mark.asyncio
async def test_another_database_error_is_not_mapped_to_ask_again(
    setup_linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = AsyncMock(side_effect=DBAPIError("grant", {}, SimpleNamespace(sqlstate="57014")))
    _patch_tool_agent(monkeypatch)
    monkeypatch.setattr(connect_tool, "linked_grant", grant)
    with pytest.raises(DBAPIError):
        await connect_tool._github_connect_impl(
            _runtime(setup_linked),
            _admin_auth(setup_linked),
            origin_context_id="synthetic",
            repo_name="acme/widgets",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
async def test_both_linked_paths_lock_the_installation_before_the_agent_fence(
    setup_linked, two_orgs, monkeypatch: pytest.MonkeyPatch, repo_name: str
) -> None:
    order: list[str] = []
    real_installation_lock = linked.get_for_update
    real_fence = github_access.lock_agent_grants

    async def installation_lock(*args, **kwargs):
        order.append("installation")
        return await real_installation_lock(*args, **kwargs)

    async def fence(*args, **kwargs):
        order.append("fence")
        return await real_fence(*args, **kwargs)

    monkeypatch.setattr(linked, "get_for_update", installation_lock)
    monkeypatch.setattr(github_access, "lock_agent_grants", fence)
    assert (await setup_linked.grant(repo_name=repo_name)).status.startswith("granted")
    assert "fence" in order and "installation" in order
    assert order.index("installation") < order.index("fence")
    assert max(i for i, step in enumerate(order) if step == "installation") < order.index("fence")
