"""Agent coding-key tools: thin over core, whose actor rules decide who may revoke."""

import uuid

import pytest
from daimon.adapters.mcp.tools import agent_coding_keys as tool
from daimon.core.agent_editing import AgentEditError
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth


@pytest.fixture(autouse=True)
def _deps(monkeypatch):
    monkeypatch.setattr(tool, "_edit_deps", lambda _runtime: object())
    monkeypatch.setattr(tool, "_edit_actor", lambda _auth: object())


async def test_revoke_when_allowed_then_names_the_key(monkeypatch):
    seen = {}

    async def fake(_deps, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(tool.coding_keys, "revoke_agent_coding_key", fake)
    key = uuid.uuid4()
    out = await tool._revoke_impl(fake_runtime(), make_auth(), "agent-1", key)
    assert (out.agent_id, out.key_id) == ("agent-1", str(key)), "the result names what was revoked"
    assert seen["jti"] == key and seen["ma_agent_id"] == "agent-1", "core gets the same key"


async def test_revoke_when_core_refuses_then_tool_error(monkeypatch):
    async def refuse(*_a, **_k):
        raise AgentEditError("not yours", kind="forbidden")

    monkeypatch.setattr(tool.coding_keys, "revoke_agent_coding_key", refuse)
    with pytest.raises(ToolError):
        await tool._revoke_impl(fake_runtime(), make_auth(admin=False), "agent-1", uuid.uuid4())


async def test_list_when_core_refuses_then_tool_error(monkeypatch):
    async def refuse(*_a, **_k):
        raise AgentEditError("not yours", kind="forbidden")

    monkeypatch.setattr(tool.coding_keys, "list_agent_coding_keys", refuse)
    with pytest.raises(ToolError):
        await tool._list_impl(fake_runtime(), make_auth(admin=False), "agent-1")
