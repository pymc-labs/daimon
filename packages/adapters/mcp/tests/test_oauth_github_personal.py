"""A personal GitHub link needs a matching chat identity and browser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.oauth_github_personal import build_personal_link_routes
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubAppSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores.github_links import account_link_status, unlink_verified_identity
from daimon.core.stores.github_personal_links import digest, get_intent, mint_link
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route


@pytest.mark.asyncio
async def test_personal_link_verifies_browser_and_discord_user(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="github-client",
            client_secret=SecretStr("github-secret"),
        ),
        hub=HubSettings(
            discord_client_id="discord-client",
            discord_client_secret=SecretStr("discord-secret"),
        ),
    )
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="123")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session,
            platform="discord",
            external_id="456",
            tenant=tenant,
            account=account,
        )
        second_tenant = await make_tenant(session, platform="discord", workspace_id="999")
        second_account = await make_account(session, tenant=second_tenant)
        await make_platform_principal(
            session,
            platform="discord",
            external_id="456",
            tenant=second_tenant,
            account=second_account,
        )
        link = await mint_link(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            platform_user_id="456",
            root_url="https://mcp.test",
        )
    actual_discord_user = "789"

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            return httpx.Response(200, json={"access_token": "discord-access"})
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": actual_discord_user})
        if request.url.path == "/login/oauth/access_token":
            assert parse_qs(request.content.decode())["code_verifier"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "github-access",
                    "refresh_token": "github-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17, "login": "carlos"})
        raise AssertionError(f"unexpected API call {request.url}")

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(api))

    start, platform_callback, github_callback = build_personal_link_routes(
        settings=settings,
        sessionmaker=committing_sessionmaker,
        fernet=build_multifernet((key,)),
        client_factory=factory,
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/link/platform-callback", platform_callback),
            Route("/oauth/github/link/{token}", start),
            Route("/oauth/github/callback", github_callback),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        opened = await browser.get(link)
        assert opened.status_code == 307
        assert urlparse(opened.headers["location"]).hostname == "discord.com"
        platform_state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
        ) as forwarded:
            refused = await forwarded.get(
                "/oauth/github/link/platform-callback",
                params={"state": platform_state, "code": "discord-code"},
            )
            assert refused.status_code == 400
        wrong_person = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "discord-code"},
        )
        assert wrong_person.status_code == 403
        actual_discord_user = "456"
        verified = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "discord-code"},
        )
        assert verified.status_code == 307
        github_state = parse_qs(urlparse(verified.headers["location"]).query)["state"][0]
        linked = await browser.get(
            "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
        )
        assert linked.status_code == 200
        assert "Linked as @carlos." in linked.text
        repeated = await browser.get(
            "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
        )
        assert repeated.status_code == 400
        async with committing_sessionmaker.begin() as session:
            expiring = await mint_link(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                platform_user_id="456",
                root_url="https://mcp.test",
            )
            old = await get_intent(
                session,
                token_hash=digest(expiring.rsplit("/", 1)[-1]),
                include_expired=True,
            )
            assert old is not None
            old.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired = await browser.get(expiring)
        assert "This link expired. Ask again in chat." in expired.text
        assert "Get a new link" not in expired.text
    async with committing_sessionmaker() as session:
        assert await account_link_status(session, account_id=account.id) == "carlos"
        assert await account_link_status(session, account_id=second_account.id) == "carlos"
    async with committing_sessionmaker.begin() as session:
        assert (
            await unlink_verified_identity(
                session,
                tenant_id=tenant.id,
                platform="discord",
                platform_user_id="456",
                account_id=account.id,
            )
            == 2
        )
    async with committing_sessionmaker() as session:
        assert await account_link_status(session, account_id=account.id) is None
        assert await account_link_status(session, account_id=second_account.id) is None


@pytest.mark.asyncio
async def test_personal_link_signs_in_with_slack_before_github(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="github-client",
            client_secret=SecretStr("github-secret"),
        ),
        hub=HubSettings(slack_client_id="slack-client", slack_client_secret=SecretStr("secret")),
    )
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id="T123")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="slack", external_id="U456", tenant=tenant, account=account
        )
        link = await mint_link(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="slack",
            platform_user_id="U456",
            root_url="https://mcp.test",
        )

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/openid.connect.token":
            return httpx.Response(200, json={"ok": True, "access_token": "slack-access"})
        if request.url.path == "/api/openid.connect.userInfo":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "https://slack.com/user_id": "U456",
                    "https://slack.com/team_id": "T123",
                },
            )
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "github-access",
                    "refresh_token": "github-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17, "login": "carlos"})
        raise AssertionError(f"unexpected API call {request.url}")

    start, platform_callback, github_callback = build_personal_link_routes(
        settings=settings,
        sessionmaker=committing_sessionmaker,
        fernet=build_multifernet((key,)),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(api)),
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/link/platform-callback", platform_callback),
            Route("/oauth/github/link/{token}", start),
            Route("/oauth/github/callback", github_callback),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        opened = await browser.get(link)
        assert opened.status_code == 307
        assert urlparse(opened.headers["location"]).hostname == "slack.com"
        assert parse_qs(urlparse(opened.headers["location"]).query)["team"] == ["T123"]
        platform_state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        verified = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "slack-code"},
        )
        assert verified.status_code == 307
        assert urlparse(verified.headers["location"]).hostname == "github.com"
        github_state = parse_qs(urlparse(verified.headers["location"]).query)["state"][0]
        linked = await browser.get(
            "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
        )
        assert linked.status_code == 200
        assert "Linked as @carlos." in linked.text
    async with committing_sessionmaker() as session:
        assert await account_link_status(session, account_id=account.id) == "carlos"
