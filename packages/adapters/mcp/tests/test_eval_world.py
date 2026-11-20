"""An eval item's calls never run a Daimon tool: the fake world answers every one.

Drives the real app: the live registry, every middleware, the `call_tool` proxy.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.middleware.eval_world import PROXY_TOOLS, EvalWorldMiddleware
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.evals.eval_tenant import eval_tenant
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.stores import eval_world
from daimon.testing.eval_world import eval_case, make_eval_principal, make_running_item
from fastmcp import Client, FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.tools.function_tool import FunctionTool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_tool_schema_snapshot import (
    _fully_configured_settings,  # pyright: ignore[reportPrivateUsage]
)

SECRET = b"e" * 32


async def test_every_tool_when_an_eval_item_calls_it_then_the_world_answers_and_no_handler_runs(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_mcp_app(
        settings=_fully_configured_settings(), sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )  # fmt: skip
    mcp: FastMCP = app.state.mcp
    world = next(m for m in mcp.middleware if isinstance(m, EvalWorldMiddleware))
    world._world.simulator = None  # pyright: ignore[reportPrivateUsage] - no model in tests
    async with sessionmaker() as session, session.begin():
        principal = await make_eval_principal(session)
    tenant_id = await eval_tenant(sessionmaker)
    async with sessionmaker() as session, session.begin():
        item_id = await make_running_item(session, tenant_id, eval_case({}))
        token, _, _ = await mint_chat_mcp_token(
            session, account_id=principal.account_id, agent_id=principal.account_id,
            secret=SECRET, now=dt.datetime.now(dt.UTC), eval_item_id=item_id,
        )  # fmt: skip
    verified = await DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker).verify_token(token)
    assert verified is not None, "the item's own credential verifies"
    ran: list[str] = []
    original = FunctionTool.run

    async def spy(self: FunctionTool, arguments: dict[str, Any]) -> Any:
        if self.name not in PROXY_TOOLS:
            ran.append(self.name)
        return await original(self, arguments)

    monkeypatch.setattr(FunctionTool, "run", spy)
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: verified
    )
    registered = sorted(t.name for t in await mcp.local_provider.list_tools())
    outcomes: dict[str, str] = {}
    async with Client(mcp) as client:
        for name in registered:
            result = await client.call_tool(name, {}, raise_on_error=False)
            outcomes[name] = " ".join(getattr(c, "text", "") for c in result.content)
        await client.call_tool("call_tool", {"name": "web_fetch", "arguments": {}},
                               raise_on_error=False)  # fmt: skip
    assert ran == [], f"handlers ran for an eval item: {ran}"
    async with sessionmaker() as session:
        logged = await eval_world.list_calls(session, item_id=item_id)
    answered = [str(c.get("tool")) for c in logged]
    refused = {n: o for n, o in outcomes.items() if n not in answered}
    assert all(o.startswith("Unknown tool") or "Nothing was done" in o for o in refused.values()), (
        f"a tool neither answered by the world nor refused before it: {refused}"
    )
    assert answered.count("web_fetch") == 2, "the call_tool proxy reaches the world too"
