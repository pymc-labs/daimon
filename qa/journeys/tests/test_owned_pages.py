"""Chromium crosses the GitHub page, form, server and committed database boundary."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from daimon.adapters.mcp import oauth_github
from daimon.adapters.mcp import server as mcp_server
from daimon.adapters.mcp.oauth_github import build_oauth_github_routes
from daimon.adapters.mcp.web_shell import STATIC_DIR
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import github_connect
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

from qa.journeys.evidence import create_artifact_dir
from qa.journeys.owned_pages import github_connect_journey

if TYPE_CHECKING:
    from playwright.async_api import Route as BrowserRoute


@pytest.mark.slow
@pytest.mark.parametrize(
    ("viewport_name", "viewport"),
    [
        ("desktop", {"width": 1280, "height": 900}),
        ("mobile", {"width": 390, "height": 844}),
    ],
)
@pytest.mark.parametrize("outcome", ["connect", "cancel"])
@pytest.mark.asyncio
async def test_github_connect_real_page_matches_committed_outcome(
    db_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    viewport_name: str,
    viewport: dict[str, int],
    outcome: str,
) -> None:
    playwright = pytest.importorskip("playwright.async_api")
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=tenant_id, workspace_id="browser-qa")
        await make_account(session, tenant=tenant, id=account_id)
        await set_role(session, account_id, Role.ADMIN)
        invitation_token = await github_connect.mint_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=account_id,
            requester_label="Alex",
            requester_platform_user_id="123",
            origin_platform="discord",
        )

    # Reserve the socket before starting uvicorn, so another local process
    # cannot claim the port between discovery and bind.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    key = Fernet.generate_key().decode()
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl(f"{base_url}/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="client",
            client_secret=SecretStr("secret"),
        ),
    )

    def fake_app_jwt(private_key: str, app_id: str, *, now: int) -> str:
        return "app-jwt"

    monkeypatch.setattr(oauth_github, "build_app_jwt", fake_app_jwt)
    github_calls: list[str] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        github_calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/login/oauth/access_token":
            assert parse_qs(request.content.decode())["code"] == ["qa-code"]
            return httpx.Response(200, json={"access_token": "qa-user-token"})
        if request.url.path == "/applications/client/token":
            assert request.method == "DELETE"
            return httpx.Response(204)
        if request.url.path == "/app/installations/77":
            assert request.headers["authorization"] == "Bearer app-jwt"
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "account": {"id": 55, "login": "example", "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": None,
                },
            )
        assert request.headers["authorization"] == "Bearer qa-user-token"
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
                            "repository_selection": "selected",
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
                            "permissions": {"admin": True},
                        },
                        {
                            "id": 102,
                            "owner": {"id": 55},
                            "full_name": "example/two",
                            "permissions": {"admin": True},
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected GitHub path {request.url.path}")

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    connect, callback, setup, confirm = build_oauth_github_routes(
        settings=settings,
        sessionmaker=sessionmaker,
        fernet=build_multifernet((key,)),
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
    app.add_route(
        "/web/daimon-face.png",
        mcp_server._web_face,  # pyright: ignore[reportPrivateUsage]
        methods=["GET", "HEAD"],
    )
    app.mount(
        "/web",
        mcp_server._WebStaticFiles(directory=STATIC_DIR),  # pyright: ignore[reportPrivateUsage]
        name="web",
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.02)

        run_id = (
            f"github-{outcome}-{viewport_name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
            f"-{uuid.uuid4().hex[:8]}"
        )
        artifact_root = (
            Path(os.environ["DAIMON_QA_ARTIFACT_ROOT"])
            if os.environ.get("DAIMON_QA_ARTIFACT_ROOT")
            else tmp_path
        )
        artifact_dir = create_artifact_dir(run_id, root=artifact_root)
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context(
                    viewport={"width": viewport["width"], "height": viewport["height"]}
                )
                blocked: list[str] = []

                async def local_only(route: BrowserRoute) -> None:
                    request_url = urlparse(route.request.url)
                    if (request_url.scheme, request_url.hostname, request_url.port) == (
                        "http",
                        "127.0.0.1",
                        port,
                    ):
                        await route.continue_()
                    else:
                        blocked.append(route.request.url)
                        await route.abort()

                await context.route("**/*", local_only)
                page = await context.new_page()
                report = await github_connect_journey(
                    page,
                    base_url=base_url,
                    invitation_token=invitation_token,
                    tenant_id=tenant_id,
                    sessionmaker=sessionmaker,
                    artifact_dir=artifact_dir,
                    browser_version=browser.version,
                    viewport=viewport,
                    outcome=outcome,
                )
                assert report.finalize() == "PASS"
                assert blocked == [], blocked
            finally:
                await browser.close()
        assert "DELETE /applications/client/token" in github_calls
        assert (artifact_dir / "github-picker.png").stat().st_size > 0
        if outcome == "connect":
            assert (artifact_dir / "github-connected.png").stat().st_size > 0
            assert (artifact_dir / "github-used-link.png").stat().st_size > 0
            assert (artifact_dir / "github-deliberate-change-diff.png").stat().st_size > 0
        else:
            assert (artifact_dir / "github-cancelled.png").stat().st_size > 0
        assert (artifact_dir / "report.json").is_file()
        print(f"owned-page QA artifacts: {artifact_dir}")
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        sock.close()
