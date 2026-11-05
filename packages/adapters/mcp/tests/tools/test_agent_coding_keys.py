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

    async def card(*_a, **_k):
        return None

    monkeypatch.setattr(tool, "require_card", card)

    async def not_mine(*_a, **_k):
        return False

    monkeypatch.setattr(tool.coding_keys, "revoke_my_coding_key", not_mine)


async def test_revoke_when_allowed_then_names_the_key(monkeypatch):
    seen = {}

    async def fake(_deps, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(tool.coding_keys, "revoke_agent_coding_key", fake)
    key = uuid.uuid4()
    out = await tool.revoke_coding_key(fake_runtime(), make_auth(), str(key), "agent-1", "ctx")
    assert out.key_id == str(key), "the result names what was revoked"
    assert seen["jti"] == key and seen["ma_agent_id"] == "agent-1", "core gets the same key"


async def test_revoke_when_core_refuses_then_tool_error(monkeypatch):
    async def refuse(*_a, **_k):
        raise AgentEditError("not yours", kind="forbidden")

    monkeypatch.setattr(tool.coding_keys, "revoke_agent_coding_key", refuse)
    with pytest.raises(ToolError):
        await tool.revoke_coding_key(
            fake_runtime(), make_auth(admin=False), str(uuid.uuid4()), "agent-1", "ctx"
        )


async def test_list_when_core_refuses_then_tool_error(monkeypatch):
    async def refuse(*_a, **_k):
        raise AgentEditError("not yours", kind="forbidden")

    monkeypatch.setattr(tool.coding_keys, "list_agent_coding_keys", refuse)
    with pytest.raises(ToolError):
        await tool._list_impl(fake_runtime(), make_auth(admin=False), "agent-1")


async def test_revoke_own_key_when_not_the_callers_then_tool_error(monkeypatch):
    async def no(*_a, **_k):
        return False

    monkeypatch.setattr(tool.coding_keys, "revoke_my_coding_key", no)
    with pytest.raises(ToolError):
        await tool.revoke_coding_key(fake_runtime(), make_auth(), str(uuid.uuid4()), None, "ctx")


async def test_revoke_own_key_when_malformed_then_same_tool_error():
    with pytest.raises(ToolError, match="No such key"):
        await tool.revoke_coding_key(fake_runtime(), make_auth(), "not-a-uuid", None, "ctx")


async def test_revoke_own_key_with_the_agent_id_list_coding_keys_gave_then_revoked_as_own(
    monkeypatch,
):
    """A member passes the agent_id their key listed; it is theirs, so no admin path runs."""

    async def mine(*_a, **_k):
        return True

    async def admin(*_a, **_k):
        raise AssertionError("the owner/admin path must not run for the caller's own key")

    monkeypatch.setattr(tool.coding_keys, "revoke_my_coding_key", mine)
    monkeypatch.setattr(tool.coding_keys, "revoke_agent_coding_key", admin)
    key = str(uuid.uuid4())
    out = await tool.revoke_coding_key(
        fake_runtime(), make_auth(admin=False), key, "agent-1", "ctx"
    )
    assert out.key_id == key, "the member's own key is revoked whatever agent_id says"
