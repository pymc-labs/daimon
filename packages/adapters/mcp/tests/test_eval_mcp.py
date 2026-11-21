"""The eval MCP route refuses a request body past the cap."""

from __future__ import annotations

import uuid

import httpx
import pytest
from daimon.adapters.mcp.eval_mcp import EVAL_MCP_ROUTE, build_eval_mcp_route
from daimon.core.evals.world import http_app
from daimon.core.evals.world.model import ItemRef
from daimon.testing.eval_world import eval_case
from starlette.applications import Starlette
from starlette.routing import Route


class _World:
    def __init__(self, item: ItemRef) -> None:
        self.item = item

    async def item_for_token(self, token: str) -> ItemRef | None:
        return self.item


async def test_eval_mcp_when_the_body_is_over_the_cap_then_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_app, "MAX_BODY", 16)
    item = ItemRef(uuid.uuid4(), uuid.uuid4(), eval_case({}), "0" * 64)
    route = build_eval_mcp_route(_World(item))  # pyright: ignore[reportArgumentType]
    app = Starlette(routes=[Route(EVAL_MCP_ROUTE, route, methods=["POST"])])
    url = f"http://test/eval-mcp/{item.id}/srv"
    headers = {"authorization": "Bearer t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        big = await client.post(url, content=b"x" * 64, headers=headers)
        small = await client.post(url, content=b"{}", headers=headers)
    assert big.status_code == 413, "a body over the cap is refused"
    assert small.status_code == 202, "a small notification is still accepted"
