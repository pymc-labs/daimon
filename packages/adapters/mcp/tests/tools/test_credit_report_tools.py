"""Credit and feedback tools: the admin gate, what each calls, and the page it links to."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.mcp.tools import _web_admin as shared
from daimon.adapters.mcp.tools import credit_reports as tool
from daimon.core import credit_report
from daimon.core.config import WebSettings
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth


@pytest.fixture
def runtime(monkeypatch):
    async def no_grant(*_a: Any, **_k: Any):
        return None

    async def agents(*_a: Any, **_k: Any):
        return {"a1": "Ada"}

    monkeypatch.setattr(shared.web_roles, "get_workspace_role", no_grant)
    monkeypatch.setattr(tool, "agent_names", agents)
    rt = fake_runtime()
    web = WebSettings(public_url="https://web.test")  # type: ignore[call-arg]
    rt.settings = SimpleNamespace(web=web, billing=SimpleNamespace(markup=Decimal(1)))  # type: ignore[misc]
    return rt


async def test_admin_reads_when_member_then_refused(runtime):
    member = make_auth(admin=False)
    for call in (
        tool._overview_impl(runtime, member),
        tool._spend_impl(runtime, member, "person", "30d"),
        tool._feedback_impl(runtime, member, 30),
    ):
        with pytest.raises(ToolError):
            await call


async def test_my_credits_when_external_agent_then_refused(runtime):
    for extra in ({"agent_id": uuid.uuid4()}, {"foreign_home_id": "other"}):
        with pytest.raises(ToolError, match="own credits"):
            await tool._mine_impl(runtime, make_auth(admin=False, **extra), "30d")


async def test_my_credits_when_member_then_core_report_and_credits_link(runtime, monkeypatch):
    seen: dict[str, Any] = {}
    auth = make_auth(admin=False)

    async def fake_mine(_sessions, **kw: Any):
        seen.update(kw)
        entry = credit_report.SpendEntry("a1", "Ada", 2, 1.5)
        return credit_report.MineReport([entry], [], 1.5, 2)

    monkeypatch.setattr(tool.credit_report, "my_credits", fake_mine)
    out = await tool._mine_impl(runtime, auth, "this")
    assert seen["platform_user_id"] == "u1" and seen["period"] == "this", "the caller's own id"
    assert [(r.name, r.usd) for r in out.by_agent] == [("Ada", 1.5)], "billed dollars by agent"
    assert out.web_url == f"https://web.test/w/{auth.tenant_id}/credits", "a member's credits page"


async def test_spend_when_admin_then_rows_and_spend_link(runtime, monkeypatch):
    auth = make_auth()

    async def fake_spend(_sessions, **kw: Any):
        assert kw["by"] == "agent", "the breakdown asked for reaches core"
        return [credit_report.SpendEntry("a1", "Ada", 3, 2.0, avatar="x")]

    monkeypatch.setattr(tool.credit_report, "spend_breakdown", fake_spend)
    out = await tool._spend_impl(runtime, auth, "agent", "30d")
    assert [(r.id, r.turns) for r in out.rows] == [("a1", 3)], "one line per agent"
    assert out.web_url is not None and out.web_url.startswith("https://web.test/"), "a link"


async def test_feedback_when_days_out_of_range_then_tool_error(runtime):
    for days in (0, 366):
        with pytest.raises(ToolError, match="days"):
            await tool._feedback_impl(runtime, make_auth(), days)
