"""Browser GitHub connection routes with a mocked GitHub API."""

from __future__ import annotations

import uuid
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
from daimon.core.stores import github_access, github_connect
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
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


@pytest.mark.asyncio
async def test_connection_happy_path_and_rechecks(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
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
            workspace_label="Example workspace",
            requester_label="Alex",
        )
    owner = True
    repo_admin = True
    requests: list[httpx.Request] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login/oauth/access_token":
            body = parse_qs(request.content.decode())
            assert body["code_verifier"][0]
            return httpx.Response(200, json={"access_token": "user-token"})
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
                            "permissions": {"pull": True},
                        },
                    ]
                },
            )
        if request.url.path == "/user/memberships/orgs":
            return httpx.Response(
                200,
                json=[
                    {
                        "organization": {"id": 55},
                        "state": "active",
                        "role": "admin" if owner else "member",
                    }
                ],
            )
        raise AssertionError(f"unexpected GitHub path {request.url.path}")

    key = Fernet.generate_key().decode()
    settings = _settings(key)
    fernet = build_multifernet((key,))

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
        assert "Example workspace" in page.text and "Alex" in page.text
        assert "example/one" in page.text and "example/two" not in page.text
        assert "All repos in this org" in page.text
        spoof = await browser.get(
            "/oauth/github/setup", params={"state": state, "installation_id": "999999"}
        )
        assert spoof.status_code == 307
        assert "installation_id" not in spoof.headers["location"]
        owner = False
        page_no_owner = await browser.get("/oauth/github/confirm", params={"state": state})
        assert "All repos in this org" not in page_no_owner.text
        owner = True
        repo_admin = False
        denied = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101", "access_101": "write"}
        )
        assert denied.status_code == 403
        async with sessionmaker() as session:
            assert await github_access.list_authorized_repos(session, tenant_id=tenant_id) == []
        repo_admin = True
        confirmed = await browser.post(
            "/oauth/github/confirm", data={"state": state, "repo": "101", "access_101": "write"}
        )
        assert confirmed.status_code == 200 and "Connected: 1 repos" in confirmed.text
        async with sessionmaker() as session:
            repos = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
            assert len(repos) == 1 and repos[0].repo_id == 101
            assert repos[0].installation_id == 77 and repos[0].max_access == "write"
        reused = await browser.post("/oauth/github/confirm", data={"state": state, "repo": "101"})
        assert reused.status_code == 400
        async with sessionmaker.begin() as session:
            org_invitation = await github_connect.mint_invitation(
                session, tenant_id=tenant_id, requester_account_id=account_id
            )
        org_start = await browser.get(f"/oauth/github/connect/{org_invitation}")
        org_state = parse_qs(urlparse(org_start.headers["location"]).query)["state"][0]
        await browser.get("/oauth/github/callback", params={"state": org_state, "code": "code"})
        org_confirmed = await browser.post(
            "/oauth/github/confirm",
            data={"state": org_state, "org": "55", "org_access_55": "read"},
        )
        assert org_confirmed.status_code == 200
        async with sessionmaker() as session:
            scope = await github_connect.get_org_scope(session, tenant_id=tenant_id, owner_id=55)
            assert scope is not None and scope.scope == "org_all"
            assert scope.installation_id == 77 and scope.max_access == "read"
    assert any(request.url.path == "/user/installations" for request in requests)
