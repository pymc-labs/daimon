"""`list_routines` resolves the caller's web links and opens a session once, not per routine."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
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
        return SimpleNamespace(platform="discord")

    async def fake_rows(_rt: Any, _auth: Any) -> list[RoutineRow]:
        return [_row() for _ in range(5)]

    async def agent_opt_in(_session: Any, rows: list[RoutineRow], _platform: str) -> dict[Any, str]:
        reads.append(len(rows))
        return {row.id: "agent" for row in rows}

    reads: list[int] = []

    opened: list[int] = []

    @contextlib.asynccontextmanager
    async def session() -> AsyncIterator[object]:
        opened.append(1)
        yield object()

    monkeypatch.setattr(routines_mod, "tenant_links", fake_links)
    monkeypatch.setattr(routines_mod, "_auth", fake_auth)
    monkeypatch.setattr(routines_mod, "_list_routines_impl", fake_rows)
    monkeypatch.setattr(routines_mod, "opted_in_by_many", agent_opt_in)
    mcp = FastMCP("t")
    runtime = SimpleNamespace(session_factory=session)
    routines_mod.register_routines_tools(mcp, cast(McpRuntime, runtime))
    tool = await mcp.get_tool("list_routines")
    out = await tool.fn(None)  # pyright: ignore[reportAttributeAccessIssue]
    assert len(out) == 5, "one view per routine"
    assert all(v.web_url and v.sandbox_tools for v in out), "each carries its link and opt-in"
    assert reads == [5], "the opt-ins of the whole list are read once"
    assert len(calls) == 1, "links are looked up once per call, not per row"
    assert len(opened) == 1, "the opt-ins are read in one session"
