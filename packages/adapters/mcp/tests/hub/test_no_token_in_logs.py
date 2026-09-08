"""Neither the upstream token nor the login claims reach a log line on a hub call."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.core.hub_identity import HubTenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.applications import Starlette

from .test_app import _lifespan, _runtime, _tools_list

pytestmark = pytest.mark.asyncio

TOKEN_SENTINEL = "hub-sentinel-upstream-token-DO-NOT-LOG"
CLAIMS_SENTINEL = "hub-sentinel-workspace"


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


async def test_hub_call_logs_neither_the_bearer_token_nor_the_login_claims(
    sessionmaker: Any, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    tenant = HubTenant(
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        workspace_id="g1",
        workspace_name=CLAIMS_SENTINEL,
    )
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[tenant])
    auth = StaticTokenVerifier(
        tokens={TOKEN_SENTINEL: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )
    mcp = build_hub_app(
        platform="discord", runtime=_runtime(sessionmaker), auth=auth, billing_config=None
    )
    caplog.set_level(logging.DEBUG)

    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    names = await _tools_list(app, "/mcp", TOKEN_SENTINEL)
    daimons = await _call_list_daimons(app, "/mcp", TOKEN_SENTINEL)
    # daimon's structlog uses PrintLoggerFactory, so its own lines land on the
    # process's stdout and never reach the stdlib logging caplog reads.
    captured = capfd.readouterr()
    printed = captured.out + captured.err

    assert "list_daimons" in names, f"expected list_daimons among hub tools, got {names!r}"
    assert daimons == [], f"empty tenant should list no daimons, got {daimons!r}"
    for haystack, where in ((caplog.text, "stdlib logs"), (printed, "stdout")):
        assert TOKEN_SENTINEL not in haystack, (
            f"bearer token must never be logged; found in {where}"
        )
        assert CLAIMS_SENTINEL not in haystack, (
            f"login claims must never be logged; found in {where}"
        )
    for record in caplog.records:
        assert TOKEN_SENTINEL not in record.getMessage() and TOKEN_SENTINEL not in repr(
            record.args
        ), record
