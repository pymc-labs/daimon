"""Hub apps mount beside /mcp, expose only hub tools, and stay absent when unconfigured."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.hub.app import build_hub_app, mount_hub_apps
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.defaults.loader import DeploymentDefault
from daimon.core.hub_identity import HubTenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

pytestmark = pytest.mark.asyncio

_HUB_TOOLS = {
    "list_daimons",
    "describe_daimon",
    "ask",
    "start_turn",
    "continue_turn",
    "get_session",
    "list_events",
    "list_my_sessions",
}


def _settings(hub: HubSettings | None = None) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(
            public_url=HttpUrl("https://t.example.com/mcp"), jwt_secret=SecretStr("x" * 32)
        ),
        hub=hub or HubSettings(),
    )


def _runtime(sessionmaker: Any) -> McpRuntime:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    return McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(router.dispatch),
        settings=_settings(),
        deployment_default=DeploymentDefault(environment_name=None),
    )


@asynccontextmanager
async def _lifespan(app: Starlette):
    async with app.router.lifespan_context(app):
        yield


async def _tools_list(app: Starlette, path: str, token: str) -> set[str]:
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        init = await c.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        assert "mcp-session-id" not in init.headers, (
            f"hub app must be stateless, got session {init.headers['mcp-session-id']!r}"
        )
        resp = await c.post(
            path,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert resp.headers["content-type"].startswith("application/json"), (
            f"expected JSON response, got {resp.headers['content-type']!r}"
        )
        return {t["name"] for t in resp.json()["result"]["tools"]}


async def test_hub_app_lists_exactly_the_hub_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = HubTenant(
        tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), workspace_id="g1", workspace_name="PyMC"
    )
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[tenant])
    auth = StaticTokenVerifier(
        tokens={"tok": {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )
    mcp = build_hub_app(
        platform="discord", runtime=_runtime(sessionmaker), auth=auth, billing_config=None
    )

    names = await _tools_list(
        mcp.http_app(path="/mcp", stateless_http=True, json_response=True), "/mcp", "tok"
    )

    assert names == _HUB_TOOLS, f"hub surface drifted: {sorted(names)}"


async def test_unconfigured_hub_mounts_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=_runtime(sessionmaker).client,
    )
    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert "/slack" not in paths and "/discord" not in paths, f"got {paths!r}"


async def test_mount_hub_apps_requires_signing_key_and_crypto(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.errors import BootstrapError

    hub = HubSettings(discord_client_id="id", discord_client_secret=SecretStr("s"))
    app = Starlette()
    with pytest.raises(BootstrapError, match="JWT_SIGNING_KEY"):
        mount_hub_apps(
            app,
            settings=_settings(hub),
            runtime=_runtime(sessionmaker),
            sessionmaker=sessionmaker,
            billing_config=None,
            fernet=None,
        )


async def test_mount_hub_apps_adds_mount_and_root_well_known_routes(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from cryptography.fernet import Fernet, MultiFernet

    key = Fernet.generate_key()
    hub = HubSettings(
        discord_client_id="id",
        discord_client_secret=SecretStr("s"),
        jwt_signing_key=SecretStr(key.decode()),
    )
    app = Starlette()
    mounted = mount_hub_apps(
        app,
        settings=_settings(hub),
        runtime=_runtime(sessionmaker),
        sessionmaker=sessionmaker,
        billing_config=None,
        fernet=MultiFernet([Fernet(key)]),
    )

    assert mounted == ["discord"], f"got {mounted!r}"
    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert "/discord" in paths, f"mount missing: {paths!r}"
    assert "/.well-known/oauth-authorization-server/discord" in paths, (
        f"path-aware discovery route missing: {paths!r}"
    )
    assert "/.well-known/oauth-protected-resource/discord/mcp" in paths, (
        f"resource metadata route missing: {paths!r}"
    )
