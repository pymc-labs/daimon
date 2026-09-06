"""Neither the upstream token nor the login claims reach a log line on a hub call."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.adapters.mcp.runtime import McpRuntime
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
from starlette.applications import Starlette

pytestmark = pytest.mark.asyncio

SENTINEL = "hub-sentinel-upstream-token-DO-NOT-LOG"

# _settings, _runtime, _lifespan and _tools_list are mirrored from
# tests/hub/test_app.py rather than imported: hub/ carries an __init__.py
# (unlike its sibling test dirs), so under --import-mode=importlib pytest
# resolves that module as "hub.test_app" and never registers a bare
# "test_app" name for `from test_app import ...` to find, even when this
# whole directory is collected together. Keep these in sync with test_app.py
# if it changes.


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(
            public_url=HttpUrl("https://t.example.com/mcp"), jwt_secret=SecretStr("x" * 32)
        ),
        hub=HubSettings(),
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


async def _call_list_daimons(app: Starlette, path: str, token: str) -> list[dict[str, Any]]:
    """Actually invoke the ``list_daimons`` tool (not just list its schema), so log
    output from tool execution -- not merely the initialize/tools-list handshake
    -- is captured too."""
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
        resp = await c.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_daimons", "arguments": {}},
            },
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        result = payload.get("result", payload)
        assert isinstance(result, dict) and not result.get("isError"), (
            f"list_daimons call failed: {result!r}"
        )
        structured = result.get("structuredContent") or {}
        items = structured.get("result", structured) if isinstance(structured, dict) else structured
        return items if isinstance(items, list) else []


async def test_hub_call_never_logs_the_bearer_token(
    sessionmaker: Any, caplog: pytest.LogCaptureFixture
) -> None:
    tenant = HubTenant(
        tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), workspace_id="g1", workspace_name="PyMC"
    )
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[tenant])
    auth = StaticTokenVerifier(
        tokens={SENTINEL: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )
    mcp = build_hub_app(
        platform="discord", runtime=_runtime(sessionmaker), auth=auth, billing_config=None
    )
    caplog.set_level(logging.DEBUG)

    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    names = await _tools_list(app, "/mcp", SENTINEL)
    daimons = await _call_list_daimons(app, "/mcp", SENTINEL)

    assert "list_daimons" in names, f"expected list_daimons among hub tools, got {names!r}"
    assert daimons == [], f"empty tenant should list no daimons, got {daimons!r}"
    assert SENTINEL not in caplog.text, "bearer token must never be logged"
    for record in caplog.records:
        assert SENTINEL not in record.getMessage() and SENTINEL not in repr(record.args), record
