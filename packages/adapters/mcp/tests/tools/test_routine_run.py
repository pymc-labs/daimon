"""run_routine hands the caller to the core call the web uses; report_routine_problem records
only for the routine signed into a live fire's credential."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import routine_run as tool
from daimon.core.lead_intake_execution import current_intake_execution
from daimon.core.ops_events import drain_ops_events
from daimon.core.routine_service import RoutineRefused
from daimon.core.stores.lead_intake import routine_execution
from daimon.core.stores.ops_events import list_issue_groups
from daimon.testing.factories import make_routine, make_tenant
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth


async def test_run_when_called_then_returns_queue_data_only(monkeypatch):
    rid = uuid.uuid4()

    async def fake_run_now(_env, _caller, routine_id):
        return SimpleNamespace(id=routine_id, next_fire_at=None)

    monkeypatch.setattr(tool.routine_service, "run_now", fake_run_now)
    monkeypatch.setattr(tool, "_env", lambda *_: object())
    monkeypatch.setattr(tool, "_caller", lambda *_: object())
    out = await tool._run_routine_impl(fake_runtime(), make_auth(admin=False), rid)
    assert out.routine_id == str(rid), "the result names the routine that was queued"


async def test_run_when_core_refuses_then_tool_error(monkeypatch):
    async def refuse(*_args):
        raise RoutineRefused("paused")

    monkeypatch.setattr(tool.routine_service, "run_now", refuse)
    monkeypatch.setattr(tool, "_env", lambda *_: object())
    monkeypatch.setattr(tool, "_caller", lambda *_: object())
    with pytest.raises(ToolError):
        await tool._run_routine_impl(fake_runtime(), make_auth(), uuid.uuid4())


async def test_report_problem_when_not_a_routine_run_then_refused():
    """A chat or outside caller has no signed routine, so nothing is recorded."""
    with pytest.raises(ToolError, match="Only a running routine"):
        await tool._report_routine_problem_impl(fake_runtime(), make_auth(), "other", "stuck")


async def test_report_problem_in_a_live_fire_then_recorded_against_the_signed_routine(
    db_session, committing_sessionmaker
):
    """The event names the routine from the credential, and only while its fire runs."""
    tenant = await make_tenant(db_session)
    routine = await make_routine(db_session, tenant=tenant, created_by_user_id="7")
    await db_session.commit()
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=committing_sessionmaker))
    async with routine_execution(committing_sessionmaker, tenant.id, routine.id, lifetime_s=60):
        grant = current_intake_execution.get()
        assert grant is not None, "every fire holds a grant"
        auth = replace(make_auth(), tenant_id=tenant.id, routine_id=grant[0], execution_id=grant[1])
        out = await tool._report_routine_problem_impl(
            runtime, auth, "blocked_access", "The report folder  is not shared."
        )
    await drain_ops_events()
    async with committing_sessionmaker() as session:
        groups = await list_issue_groups(
            session, tenant_id=tenant.id, since=datetime.now(UTC) - timedelta(hours=1)
        )
    assert out.routine_id == str(routine.id), "the tool names the signed routine"
    assert [
        (g.latest.code, g.latest.routine_id, g.latest.reason, g.latest.note) for g in groups
    ] == [
        ("routine.goal_unmet", routine.id, "blocked_access", "The report folder is not shared.")
    ], "one event for the routine, its category and the agent's one-line note"
    with pytest.raises(ToolError):
        await tool._report_routine_problem_impl(runtime, auth, "other", "late")
