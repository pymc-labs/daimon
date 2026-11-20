"""An eval item's calls never run a Daimon tool: the fake world answers every one.

Drives the real app: the live registry, every middleware, the `call_tool` proxy.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from daimon.adapters.mcp.middleware.eval_world import PROXY_TOOLS, EvalWorldMiddleware
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.evals.eval_tenant import eval_tenant
from daimon.core.evals.world.dispatch import eval_token_label
from daimon.core.stores import eval_world
from daimon.testing.eval_world import eval_case, make_running_item
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.tools.function_tool import FunctionTool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_tool_schema_snapshot import (
    _fully_configured_settings,  # pyright: ignore[reportPrivateUsage]
)


def _as_eval_item(monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, label: str) -> None:
    claims = {"sub": str(uuid.uuid4()), "role": "admin", "tenant_id": str(tenant_id),
              "platform": "web", "daimon_token_label": label}  # fmt: skip
    token = AccessToken(token="t", client_id="c", scopes=[], claims=claims)
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: token
    )


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
    tenant_id = await eval_tenant(sessionmaker)
    async with sessionmaker() as session, session.begin():
        item_id = await make_running_item(session, tenant_id, eval_case({}))
    ran: list[str] = []
    original = FunctionTool.run

    async def spy(self: FunctionTool, arguments: dict[str, Any]) -> Any:
        if self.name not in PROXY_TOOLS:
            ran.append(self.name)
        return await original(self, arguments)

    monkeypatch.setattr(FunctionTool, "run", spy)
    _as_eval_item(monkeypatch, tenant_id, eval_token_label(item_id))
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
    assert len(answered) > len(refused), f"most tools reach the world: {len(answered)}"
