"""The main MCP endpoint is stateless: any instance answers any request.

A session id held in one process's memory stranded clients after a redeploy or
on a second instance ("server terminated the MCP session", 404). These run the
production factory over `httpx.ASGITransport` against real Postgres.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import httpx
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.stores import accounts
from daimon.core.stores.domain import Role
from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, asgi_lifespan, parse_jsonrpc_response
from daimon.testing.factories import make_account, make_tenant
from fastmcp import Context, FastMCP
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.types import ASGIApp

from .harness import make_jwt

_LIST: dict[str, object] = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
_SEARCH: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "search_tools", "arguments": {"query": "archive agent"}},
}


def _app(sessionmaker: async_sessionmaker[AsyncSession]) -> Starlette:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )


@contextlib.asynccontextmanager
async def _client(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with asgi_lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _tokens(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    """(admin, member) tokens in one tenant."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="guild-stateless")
        admin = await make_account(s, tenant=tenant)
        await accounts.set_role(s, admin.id, Role.ADMIN)
        member = await make_account(s, tenant=tenant)
    return make_jwt(account_id=admin.id), make_jwt(account_id=member.id)


async def test_a_call_on_another_instance_with_its_session_id_succeeds(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, token = await _tokens(sessionmaker)
    headers = {**INIT_HEADERS, "Authorization": f"Bearer {token}"}
    first, second = _app(sessionmaker), _app(sessionmaker)

    async with _client(first) as c:
        init = await c.post("/mcp", json=INIT_BODY, headers=headers)
    assert init.status_code == 200, init.text
    assert "mcp-session-id" not in init.headers, "a stateless server issues no session id"

    async with _client(second) as c:
        stale = await c.post("/mcp", json=_LIST, headers={**headers, "Mcp-Session-Id": "gone"})
        fresh = await c.post("/mcp", json=_LIST, headers=headers)
    for response in (stale, fresh):
        assert response.status_code == 200, f"no initialize, unknown session: {response.text}"
        assert response.headers["content-type"].startswith("application/json")
        tools = {t["name"] for t in response.json()["result"]["tools"]}
        assert {"search_tools", "call_tool"} <= tools, "the request must be served in full"


async def test_admin_visibility_does_not_carry_over_on_a_shared_session_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # fastmcp keys the visibility rules by the client's session id header: an
    # admin's request on it must not widen a member's later request on it.
    admin, member = await _tokens(sessionmaker)
    app = _app(sessionmaker)

    async def found(token: str) -> str:
        headers = {**INIT_HEADERS, "Authorization": f"Bearer {token}", "Mcp-Session-Id": "shared"}
        response = await c.post("/mcp", json=_SEARCH, headers=headers)
        assert response.status_code == 200, response.text
        return str(parse_jsonrpc_response(response)["result"])

    async with _client(app) as c:
        admin_result = await found(admin)
        member_result = await found(member)
    assert "archive_agent" in admin_result, "an admin finds admin tools with no initialize"
    assert "archive_agent" not in member_result, "a member never sees an earlier admin's tools"


async def test_visibility_rules_do_not_accumulate_on_a_resent_session_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    admin, _ = await _tokens(sessionmaker)
    app = _app(sessionmaker)
    mcp: FastMCP = app.state.mcp

    @mcp.tool
    async def count_visibility_rules(ctx: Context) -> int:  # pyright: ignore[reportUnusedFunction]
        # fastmcp's own state key for the rules enable_components appends.
        return len(await ctx.get_state("_visibility_rules") or [])

    call: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "count_visibility_rules", "arguments": {}},
    }
    headers = {**INIT_HEADERS, "Authorization": f"Bearer {admin}", "Mcp-Session-Id": "resent"}
    counts: list[int] = []
    async with _client(app) as c:
        for _ in range(3):
            response = await c.post("/mcp", json=call, headers=headers)
            assert response.status_code == 200, response.text
            counts.append(response.json()["result"]["structuredContent"]["result"])
    assert counts[0] == counts[1] == counts[2], f"rules must not pile up per request: {counts}"
