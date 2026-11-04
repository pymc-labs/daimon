"""The caller's own data tools: only a person, only their own, counts as data."""

import uuid

import pytest
from daimon.adapters.mcp.tools import my_data as tool
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth


@pytest.fixture(autouse=True)
def card_confirmed(monkeypatch):
    """These tests cover the data calls; `test_card_gate` covers the person's card."""

    async def confirmed(*_a, **_k):
        return None

    monkeypatch.setattr(tool, "require_card", confirmed)


@pytest.mark.parametrize("extra", [{"foreign_home_id": "other-org"}, {"agent_id": uuid.uuid4()}])
async def test_forget_when_not_a_person_then_refused(extra, monkeypatch):
    called = []

    async def fake(*_a, **_k):
        called.append(1)
        return 1

    monkeypatch.setattr(tool.profile, "forget_personal_memory", fake)
    with pytest.raises(ToolError):
        await tool._forget_impl(fake_runtime(), make_auth(**extra), str(uuid.uuid4()))
    assert not called, "a refused caller must not reach the core call"


async def test_forget_when_person_then_scoped_to_their_account(monkeypatch):
    seen = {}

    async def fake(_session, _client, **kwargs):
        seen.update(kwargs)
        return 3

    monkeypatch.setattr(tool.profile, "forget_personal_memory", fake)
    auth, agent = make_auth(admin=False), uuid.uuid4()
    out = await tool._forget_impl(fake_runtime(), auth, str(agent))
    assert (out.action, out.removed) == ("forgot_memory", 3), "the count of removed files is data"
    assert seen["account_id"] == auth.account_id and seen["agent_id"] == agent, (
        "the call is scoped to the caller's own account"
    )


async def test_disconnect_when_nothing_connected_then_tool_error(monkeypatch):
    async def none(*_a, **_k):
        return 0

    monkeypatch.setattr(tool.profile, "disconnect_token", none)
    with pytest.raises(ToolError):
        await tool._disconnect_impl(
            fake_runtime(), make_auth(), kind="slack", agent_id=None, key=None
        )


async def test_disconnect_when_mcp_without_key_then_tool_error():
    with pytest.raises(ToolError):
        await tool._disconnect_impl(
            fake_runtime(), make_auth(), kind="mcp", agent_id=None, key=None
        )


async def test_disconnect_when_connected_then_counts(monkeypatch):
    async def one(*_a, **_k):
        return 1

    monkeypatch.setattr(tool.profile, "disconnect_token", one)
    out = await tool._disconnect_impl(
        fake_runtime(), make_auth(), kind="microsoft", agent_id=None, key=None
    )
    assert (out.action, out.removed) == ("disconnected", 1), "a disconnect reports what went"


async def test_revoke_key_when_not_the_callers_then_tool_error(monkeypatch):
    async def no(*_a, **_k):
        return False

    monkeypatch.setattr(tool.coding_keys, "revoke_my_coding_key", no)
    with pytest.raises(ToolError):
        await tool._revoke_key_impl(fake_runtime(), make_auth(), str(uuid.uuid4()))


async def test_revoke_key_when_malformed_then_same_tool_error():
    with pytest.raises(ToolError, match="Nothing like that"):
        await tool._revoke_key_impl(fake_runtime(), make_auth(), "not-a-uuid")
