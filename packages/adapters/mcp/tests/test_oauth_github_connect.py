"""Browser GitHub connection routes with a mocked GitHub API."""

from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.oauth_github import build_oauth_github_routes
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.session_snapshot import SessionSnapshot, fingerprint_identity
from daimon.core.stores import github_access, github_app_installations, github_connect
from daimon.core.stores.accounts import set_external, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_account, make_tenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route


def _settings(key: str, *, configured: bool = True) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=(
            GithubAppSettings(
                app_id="42",
                app_slug="sample-app",
                private_key=SecretStr("pem"),
                client_id="client",
                client_secret=SecretStr("secret"),
            )
            if configured
            else GithubAppSettings()
        ),
    )


def test_routes_not_mounted_when_unconfigured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    app = create_mcp_app(
        settings=_settings(key, configured=False),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    paths = [route.path for route in app.routes if isinstance(route, Route)]
    assert "/oauth/github/connect/{token}" not in paths
    assert "/oauth/github/confirm" not in paths


@pytest.mark.parametrize("agent_bound", [False, True])
@pytest.mark.asyncio
async def test_connection_happy_path_and_rechecks(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    agent_bound: bool,
) -> None:
    sessionmaker = committing_sessionmaker
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="workspace")
        await make_account(session, tenant=tenant, id=account_id)
        await set_role(session, account_id, Role.ADMIN)
        invitation_token = await github_connect.mint_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=account_id,
            requester_label="Alex",
            agent_id=(
                derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ma-agent")
                if agent_bound
                else None
            ),
            agent_name="ResearchBot" if agent_bound else None,
        )
    repo_admin = True
    repo_two_admin = False
    installation_available = True
    requests: list[httpx.Request] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login/oauth/access_token":
            body = parse_qs(request.content.decode())
            assert body["code_verifier"][0]
            return httpx.Response(200, json={"access_token": "user-token"})
        if request.url.path == "/applications/client/token":
            assert request.method == "DELETE"
            assert request.headers["authorization"].startswith("Basic ")
            assert request.content == b'{"access_token":"user-token"}'
            return httpx.Response(204)
        if request.url.path == "/app/installations/77":
            assert request.headers["authorization"] == "Bearer app-jwt"
            if not installation_available:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "account": {"id": 55, "login": "example", "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": None,
                },
            )
        assert request.headers["authorization"] == "Bearer user-token"
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 77,
                            "account": {"id": 55, "login": "example", "type": "Organization"},
                        }
                    ]
                },
            )
        if request.url.path == "/user/installations/77/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 101,
                            "owner": {"id": 55},
                            "full_name": "example/one",
                            "permissions": {"admin": repo_admin},
                        },
                        {
                            "id": 102,
                            "owner": {"id": 55},
                            "full_name": "example/two",
                            "permissions": {"pull": True, "admin": repo_two_admin},
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub path {request.url.path}")

    key = Fernet.generate_key().decode()
    settings = _settings(key)
    fernet = build_multifernet((key,))
    monkeypatch.setattr(
        "daimon.adapters.mcp.oauth_github.build_app_jwt",
        lambda *_args, **_kwargs: "app-jwt",
    )

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    connect, callback, setup, confirm = build_oauth_github_routes(
        settings=settings,
        sessionmaker=sessionmaker,
        fernet=fernet,
        client_factory=client_factory,
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/connect/{token}", connect),
            Route("/oauth/github/callback", callback),
            Route("/oauth/github/setup", setup),
            Route("/oauth/github/confirm", confirm, methods=["GET", "POST"]),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        start = await browser.get(f"/oauth/github/connect/{invitation_token}")
        assert start.status_code == 307
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        assert parse_qs(urlparse(start.headers["location"]).query)["code_challenge_method"] == [
            "S256"
        ]
        assert (
            await browser.get("/oauth/github/callback", params={"state": "bad", "code": "code"})
        ).status_code == 400
        wrong_browser = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
        )
        async with wrong_browser:
            assert (
                await wrong_browser.get(
                    "/oauth/github/callback", params={"state": state, "code": "code"}
                )
            ).status_code == 400
        callback_response = await browser.get(
            "/oauth/github/callback", params={"state": state, "code": "code"}
        )
        assert callback_response.status_code == 307
        page = await browser.get("/oauth/github/confirm", params={"state": state})
        assert page.status_code == 200
        assert "discord workspace workspace" in page.text and "Alex" in page.text
        assert 'action="https://mcp.test/oauth/github/confirm"' in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["referrer-policy"] == "no-referrer"
        malformed = await browser.post(
            "/oauth/github/confirm",
            content=b"\xff",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert malformed.status_code == 400
        assert malformed.headers["cache-control"] == "no-store"
        assert malformed.headers["x-frame-options"] == "DENY"
        assert malformed.headers["referrer-policy"] == "no-referrer"
        assert "example/one" in page.text and "example/two" not in page.text
        assert "Select all repos you administer" in page.text
        assert "onclick=" not in page.text
        assert 'id="select-all-repos"' in page.text
        assert "All repos in this org" not in page.text
        assert 'name="repo" value="101"' in page.text
        assert 'name="repo" value="101" checked' not in page.text
        assert '<option value="write" selected>' in page.text
        assert "Push branches, open issues and pull requests." in page.text
        assert "Read code, issues and pull requests." in page.text
        assert 'id="connect-repos"' in page.text
        assert 'button.textContent = "Connecting…"' in page.text
        spoof = await browser.get(
            "/oauth/github/setup", params={"state": state, "installation_id": "999999"}
        )
        assert spoof.status_code == 307
        assert "installation_id" not in spoof.headers["location"]
        repo_admin = False
        denied = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101", "access_101": "write"}
        )
        assert denied.status_code == 403
        async with sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        repo_admin = True
        repo_two_admin = True
        async with sessionmaker.begin() as session:
            await set_role(session, account_id, Role.USER)
        demoted = await browser.post("/oauth/github/confirm", data={"state": state, "repo": "101"})
        assert demoted.status_code == 400
        async with sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        async with sessionmaker.begin() as session:
            await set_role(session, account_id, Role.ADMIN)
            await set_external(session, account_id, True)
        external = await browser.get("/oauth/github/confirm", params={"state": state})
        assert external.status_code == 400
        async with sessionmaker.begin() as session:
            await set_external(session, account_id, False)
            await set_role(session, account_id, Role.ADMIN)
        installation_available = False
        unavailable = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101"}
        )
        assert unavailable.status_code == 502
        async with sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        installation_available = True
        if agent_bound:
            with monkeypatch.context() as patch:
                patch.setattr(
                    github_connect,
                    "activate_confirmed_agent",
                    AsyncMock(side_effect=ValueError("stale grant")),
                )
                failed = await browser.post(
                    "/oauth/github/confirm", data={"state": state, "repo": "101"}
                )
            assert failed.status_code == 400
            assert "GitHub connection" in failed.text
            async with sessionmaker() as session:
                assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        confirmed = await browser.post(
            "/oauth/github/confirm",
            data={
                "state": state,
                "repo": ["101", "102"],
                "access_102": "read",
            },
        )
        assert confirmed.status_code == 200
        assert (
            "Connected 2 repos to ResearchBot." if agent_bound else "Connected: 2 repos"
        ) in confirmed.text
        async with sessionmaker() as session:
            repos = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            assert {repo.repo_id: repo.max_access for repo in repos} == {101: "write", 102: "read"}
            if agent_bound:
                agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ma-agent")
                grants = await github_access.list_agent_grants(
                    session, tenant_id=tenant_id, agent_id=agent_id
                )
                assert {grant.repo_id: grant.ceiling_access for grant in grants} == {
                    101: "write",
                    102: "read",
                }
                assert all(not grant.staged for grant in grants)
                assert (
                    await github_access.get_agent_mode(
                        session, tenant_id=tenant_id, agent_id=agent_id
                    )
                    == "app"
                )
                existing = SessionSnapshot(
                    ma_agent_id="ma-agent",
                    model_id="model",
                    system_sha256=None,
                    skills_sha256="skills",
                    environment_id="env",
                    repo_url=None,
                    repo_branch=None,
                    memory_store_id=None,
                    vault_id="vault",
                    tools_sha256="tools",
                    mcp_servers_sha256="mcp",
                    env_sha256=None,
                    agent_version=1,
                    agent_name="ResearchBot",
                )
                next_turn = existing.model_copy(
                    update={
                        "github_mode": "app",
                        "repo_urls": ("example/one", "example/two"),
                    }
                )
                assert fingerprint_identity(existing) != fingerprint_identity(next_turn)
            assert all(repo.installation_id == 77 for repo in repos)
            installation = await github_app_installations.get(session, installation_id=77)
            assert installation is not None
            assert installation.app == "github_app"
            assert installation.account_login == "example"
            assert installation.account_id == 55
            assert installation.account_type == "Organization"
            assert installation.repository_selection == "selected"
            assert set(installation.repo_full_names) == {"example/one", "example/two"}
            assert (
                await github_app_installations.get_for_repo(session, repo_full_name="example/one")
                is None
            )
            events = await list_events(session, tenant_id=tenant_id)
            confirmed_events = [event for event in events if event.operation == "github_connect"]
            assert len(confirmed_events) == 1
            assert confirmed_events[0].github_repo_ids == [101, 102]
        reused = await browser.post("/oauth/github/confirm", data={"state": state, "repo": "101"})
        assert reused.status_code == 200
        assert "Already connected: 2 repos" in reused.text
        refreshed = await browser.get("/oauth/github/confirm", params={"state": state})
        assert "Already connected: 2 repos" in refreshed.text
        receipt = re.search(r'name="receipt" value="([a-f0-9]+)"', page.text)
        invitation = re.search(r'name="invitation" value="([a-f0-9]+)"', page.text)
        assert receipt is not None and invitation is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
        ) as no_cookie_browser:
            queued_post = await no_cookie_browser.post(
                "/oauth/github/confirm",
                data={
                    "state": state,
                    "repo": "101",
                    "invitation": invitation.group(1),
                    "receipt": receipt.group(1),
                },
            )
        assert queued_post.status_code == 200
        assert "Already connected: 2 repos" in queued_post.text
    assert any(request.url.path == "/user/installations" for request in requests)
    assert all(request.url.path != "/user/memberships/orgs" for request in requests)
    assert sum(request.url.path == "/applications/client/token" for request in requests) == (
        2 if agent_bound else 1
    )
