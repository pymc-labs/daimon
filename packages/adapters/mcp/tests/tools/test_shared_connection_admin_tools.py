"""The shared connections page in chat: admin only, changes on the card, the list answers.

The store and the card are stood in for (`test_card_gate` covers the card itself); held here is
each tool's own rule and the call it makes into `daimon.core.shared_connection_admin`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from daimon.adapters.mcp.tools import shared_connection_admin as tools
from daimon.core import shared_connection_admin as admin_ops
from daimon.core.web_admin import WebAdmin
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

_ID = str(uuid.uuid4())


class _Ctx:
    async def get_state(self, key: str) -> object:
        return make_auth() if key == "auth" else None


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.role = "admin"
        self.card_ok = True
        self.calls: list[tuple[str, dict[str, Any]]] = []

        async def actor(*_: object) -> WebAdmin:
            return WebAdmin(tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), role=self.role)  # type: ignore[arg-type]

        async def card(*_: object, **__: object) -> None:
            if not self.card_ok:
                raise ToolError("Confirm this in your chat conversation. Nothing changed.")

        async def on(*_: object) -> bool:
            return True

        monkeypatch.setattr(tools, "web_admin_of", actor)
        monkeypatch.setattr(tools, "require_card", card)
        monkeypatch.setattr(admin_ops, "is_on", on)
        for name in ("listing", "change_tool", "edit"):
            monkeypatch.setattr(admin_ops, name, self.record(name))

    def record(self, name: str) -> Any:
        async def call(*_: object, **kwargs: Any) -> admin_ops.Connections:
            self.calls.append((name, kwargs))
            return admin_ops.Connections(enabled=True, connections=[])

        return call

    async def call(self, tool: str, **args: Any) -> Any:
        mcp = FastMCP("t")
        runtime = fake_runtime()
        runtime.fernet = object()  # pyright: ignore[reportAttributeAccessIssue]
        tools.register_shared_connection_admin_tools(mcp, runtime)
        found: dict[str, Any] = {t.name: t for t in await mcp.local_provider.list_tools()}
        return await found[tool].fn(ctx=_Ctx(), **args)


_TURN_OFF = {"action": "turn_off", "connection": _ID, "tool": "send", "origin_context_id": "o"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


async def test_a_member_is_refused(rig: Rig) -> None:
    rig.role = "member"
    for tool, args in (
        ("list_shared_connections", {}),
        ("change_shared_connection_tool", _TURN_OFF),
    ):
        with pytest.raises(ToolError, match="workspace admin or owner"):
            await rig.call(tool, **args)
    assert not rig.calls, "a member reaches neither the list nor a change"


async def test_a_tool_turns_off_only_on_the_card(rig: Rig) -> None:
    rig.card_ok = False
    with pytest.raises(ToolError, match="Nothing changed"):
        await rig.call("change_shared_connection_tool", **_TURN_OFF)
    assert not rig.calls, "without the card nothing changes"

    rig.card_ok = True
    await rig.call("change_shared_connection_tool", **_TURN_OFF)
    assert [(n, kw["name"], kw["enabled"]) for n, kw in rig.calls] == [
        ("change_tool", "send", False)
    ], f"the card's press turns the tool off for the workspace: {rig.calls}"


async def test_the_list_answers_without_a_card(rig: Rig) -> None:
    rig.card_ok = False
    listed = await rig.call("list_shared_connections")
    assert listed.enabled and [n for n, _ in rig.calls] == ["listing"], "a read needs no card"
