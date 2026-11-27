"""The shared connections page in chat: admin only, changes on the card, the list answers.

The store and the card are stood in for (`test_card_gate` covers the card itself); held here is
each tool's own rule and the call it makes into `daimon.core.shared_connection_admin`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.tools import _private_values
from daimon.adapters.mcp.tools import shared_connection_admin as tools
from daimon.core import shared_connection_admin as admin_ops
from daimon.core import shared_connections
from daimon.core.web_admin import WebAdmin
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

_ID = str(uuid.uuid4())
KEYS = MultiFernet([Fernet(Fernet.generate_key())])
ASKER = make_auth()


class _Ctx:
    async def get_state(self, key: str) -> object:
        return ASKER if key == "auth" else None


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.role = "admin"
        self.card_ok = True
        self.on = True
        self.details = "shown"
        self.forms: list[dict[str, Any]] = []
        self.found: admin_ops.Connection | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

        async def actor(*_: object) -> WebAdmin:
            return WebAdmin(tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), role=self.role)  # type: ignore[arg-type]

        async def card(*_: object, **__: object) -> None:
            if not self.card_ok:
                raise ToolError("Confirm this in your chat conversation. Nothing changed.")

        async def on(*_: object) -> bool:
            return self.on

        async def details(*_: object) -> str:
            return self.details

        async def form(*_: object, **kwargs: Any) -> SimpleNamespace:
            self.forms.append(kwargs)
            return SimpleNamespace(instruction="Tell them to fill in the form.")

        monkeypatch.setattr(tools, "web_admin_of", actor)
        monkeypatch.setattr(tools, "require_card", card)

        async def found(*_: object) -> SimpleNamespace:
            return SimpleNamespace(id=uuid.UUID(_ID))

        monkeypatch.setattr(shared_connections, "find_connection", found)
        monkeypatch.setattr(_private_values, "private_details", details)
        monkeypatch.setattr(tools, "request_data_source_credential", form)
        monkeypatch.setattr(admin_ops, "is_on", on)
        for name in ("listing", "change_tool", "edit", "remove", "add", "refresh", "view"):
            monkeypatch.setattr(admin_ops, name, self.record(name))

    def record(self, name: str) -> Any:
        async def call(*_: object, **kwargs: Any) -> object:
            self.calls.append((name, kwargs))
            return _LISTED if name == "listing" else self.found or _CONNECTION

        return call

    async def call(self, name: str, /, **args: Any) -> Any:
        mcp = FastMCP("t")
        runtime = fake_runtime()
        runtime.fernet = KEYS  # pyright: ignore[reportAttributeAccessIssue]
        tools.register_shared_connection_admin_tools(mcp, runtime)
        found: dict[str, Any] = {t.name: t for t in await mcp.local_provider.list_tools()}
        return await found[name].fn(ctx=_Ctx(), **args)


_TOOL = admin_ops.ConnectionTool(
    name="send", description="Sends a note.", seen_description="Sends anything, unread.",
    seen_schema={"type": "object"}, seen="d", changed=True, enabled=True, writes=True, scope=None,
)  # fmt: skip
_CONNECTION = admin_ops.Connection(
    id=_ID, name="crm", url="https://crm.example/mcp", status="error", error="upstream said no",
    note=None, refreshed_at=None, scope=None, declared=False, has_token=True, tools=[_TOOL],
)  # fmt: skip
_LISTED = admin_ops.Connections(enabled=True, connections=[_CONNECTION])
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
        with pytest.raises(ToolError, match="an admin or an owner"):
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
    assert listed["enabled"] and [n for n, _ in rig.calls] == ["listing"], "a read needs no card"


async def test_a_room_gets_placeholders_and_the_askers_dm_opens_them(rig: Rig) -> None:
    rig.details = "direct_message"
    listed = await rig.call("list_shared_connections", origin_context_id="o")
    [shown] = listed["connections"]
    assert "crm.example" not in str(listed) and "upstream said no" not in str(listed), (
        f"a room's model sees no address or error: {shown}"
    )
    text, opened = _private_values.open_placeholders(
        f"{shown['url']} ({shown['error']})", ASKER, KEYS
    )
    assert (text, opened) == ("https://crm.example/mcp (upstream said no)", 2), (
        "send_private_details opens both for the asker"
    )
    assert _private_values.open_placeholders(shown["url"], make_auth(), KEYS)[1] == 0, (
        "and for no one else"
    )
    assert shown["tools"][0]["description"] == "Sends a note."

    rig.details = "shown"
    [shown] = (await rig.call("list_shared_connections", origin_context_id="o"))["connections"]
    assert shown["url"] == _CONNECTION.url, "a private chat shows the address"
    assert not {"seen_description", "seen_schema"} & set(shown["tools"][0]), (
        "words waiting for review show only on the turn_on card"
    )


async def test_a_change_in_a_room_answers_with_placeholders(rig: Rig) -> None:
    rig.details = "direct_message"
    changed = await rig.call("change_shared_connection_tool", **_TURN_OFF)
    assert [n for n, _ in rig.calls] == ["change_tool"], "the change was made"
    assert changed["name"] == "crm" and "crm.example" not in str(changed), changed


async def test_a_token_goes_only_through_the_askers_private_form(rig: Rig) -> None:
    added = await rig.call(
        "change_shared_connection", action="add", name="crm", url="https://crm.example/mcp",
        origin_context_id="o", with_token=True,
    )  # fmt: skip
    assert [n for n, _ in rig.calls] == ["add"] and not rig.calls[0][1]["token"], (
        "the connection is stored with no token"
    )
    assert added["credential_form"] and rig.forms[0]["kind"] == "data_connection"
    assert rig.forms[0]["source_id"] == uuid.UUID(_ID), "the form names the new connection"

    rig.calls.clear()
    again = await rig.call(
        "change_shared_connection", action="refresh", connection="crm", origin_context_id="o",
        with_token=True,
    )  # fmt: skip
    assert again == {"credential_form": "Tell them to fill in the form."}
    assert [n for n, _ in rig.calls] == ["view"], "the form's submit reads the tools, not this call"

    declared = _CONNECTION.model_copy(update={"declared": True})
    rig.found = declared
    with pytest.raises(ToolError, match="configuration"):
        await rig.call(
            "change_shared_connection", action="refresh", connection="crm",
            origin_context_id="o", with_token=True,
        )  # fmt: skip
    assert len(rig.forms) == 2, "a connection from configuration takes no token form"


async def test_removal_works_with_the_feature_off(rig: Rig) -> None:
    rig.on = False
    with pytest.raises(ToolError, match="turned off"):
        await rig.call("change_shared_connection_tool", **_TURN_OFF)
    removed = await rig.call(
        "change_shared_connection", action="remove", connection="crm", origin_context_id="o"
    )
    assert removed == {"removed": True} and [n for n, _ in rig.calls] == ["remove"], (
        "as on the web, a connection can be removed while the feature is off"
    )
