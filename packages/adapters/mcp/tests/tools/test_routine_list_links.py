"""`list_routines` resolves the caller's web links once, not once per routine."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import daimon.adapters.mcp.tools.routines as routines_mod
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.stores.domain import RoutineRow
from daimon.core.web_links import TenantLinks
from fastmcp import FastMCP

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _row() -> RoutineRow:
    return RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(int=1),
        created_by_user_id="u1",
        agent_id="a1",
        agent_name="Ada",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="hi",
        enabled=True,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_list_routines_when_many_rows_then_links_resolved_once(monkeypatch) -> None:
    links = TenantLinks(
        base="https://web.test",
        tenant_id=uuid.UUID(int=1),
        modules=frozenset({"routines"}),
        admin=True,
    )
    calls: list[int] = []

    async def fake_links(*_a: Any, **_k: Any) -> TenantLinks:
        calls.append(1)
        return links

    async def fake_auth(_ctx: Any) -> Any:
        return SimpleNamespace()

    async def fake_rows(_rt: Any, _auth: Any) -> list[RoutineRow]:
        return [_row() for _ in range(5)]

    monkeypatch.setattr(routines_mod, "tenant_links", fake_links)
    monkeypatch.setattr(routines_mod, "_auth", fake_auth)
    monkeypatch.setattr(routines_mod, "_list_routines_impl", fake_rows)
    mcp = FastMCP("t")
    routines_mod.register_routines_tools(mcp, cast(McpRuntime, SimpleNamespace()))
    tool = await mcp.get_tool("list_routines")
    out = await tool.fn(None)  # pyright: ignore[reportAttributeAccessIssue]
    assert len(out) == 5, "one view per routine"
    assert all(v.web_url for v in out), "each carries its link"
    assert len(calls) == 1, "links are looked up once per call, not per row"
