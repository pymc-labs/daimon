"""Thread override tools: the admin gate, the core call and the stale case."""

import pytest
from daimon.adapters.mcp.tools import thread_overrides as tool
from daimon.core.channel_settings import ThreadOverride
from daimon.core.errors import StaleChannelValue
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth


async def test_clear_when_member_then_refused_without_calling_core(monkeypatch):
    calls = []

    async def fake_clear(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tool, "clear_thread_override", fake_clear)
    with pytest.raises(ToolError):
        await tool._clear_impl(
            fake_runtime(), make_auth(admin=False), channel_id="c", thread_id="t", kind="handoff"
        )
    assert not calls, "a member must never reach the core call"


async def test_clear_when_admin_then_calls_core_for_the_callers_platform(monkeypatch):
    calls = []

    async def fake_clear(_factory, _actor, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tool, "clear_thread_override", fake_clear)
    out = await tool._clear_impl(
        fake_runtime(), make_auth(platform="slack"), channel_id="c", thread_id="t", kind="handoff"
    )
    assert out.cleared is True, "a cleared override reports cleared"
    assert calls == [
        {"platform": "slack", "channel_id": "c", "thread_id": "t", "kind": "handoff"}
    ], "the core call must get the caller's platform and the named thread"


async def test_clear_when_nothing_set_then_reports_not_cleared(monkeypatch):
    async def stale(*_args, **_kwargs):
        raise StaleChannelValue("participation", "null")

    monkeypatch.setattr(tool, "clear_thread_override", stale)
    out = await tool._clear_impl(
        fake_runtime(), make_auth(), channel_id="c", thread_id="t", kind="participation"
    )
    assert out.cleared is False, "a missing override is data, not an error"


async def test_list_when_member_then_refused():
    with pytest.raises(ToolError):
        await tool._list_impl(fake_runtime(), make_auth(admin=False), "c")


async def test_list_when_admin_then_returns_entries(monkeypatch):
    async def fake_list(_session, **_kwargs):
        return [ThreadOverride(thread_id="t", kind="handoff", value="on")]

    monkeypatch.setattr(tool, "list_thread_overrides", fake_list)
    out = await tool._list_impl(fake_runtime(), make_auth(), "c")
    assert [(o.thread_id, o.kind, o.value) for o in out.overrides] == [("t", "handoff", "on")], (
        "each override is listed with its kind and value"
    )


async def test_list_when_over_25_then_first_25_with_total_and_more(monkeypatch):
    """A channel with many thread settings lists 25 and says more exist."""

    async def fake_list(_session, **_kwargs):
        return [ThreadOverride(thread_id=f"t{n}", kind="handoff", value="on") for n in range(30)]

    monkeypatch.setattr(tool, "list_thread_overrides", fake_list)
    out = await tool._list_impl(fake_runtime(), make_auth(), "c")
    assert (len(out.overrides), out.total, out.more) == (25, 30, True), "cut at 25 of 30"
