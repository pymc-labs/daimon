"""run_routine: hands the caller to the same core call the web uses; errors become tool errors."""

import uuid
from types import SimpleNamespace

import pytest
from daimon.adapters.mcp.tools import routine_run as tool
from daimon.core.routine_service import RoutineRefused
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
