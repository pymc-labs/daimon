"""The chat tools for the data pages: admin only, carded changes, and reads that answer.

The store and the card are stood in for (`test_card_gate` covers the card itself); what is
held here is each tool's own rule and the call it makes into `daimon.data`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.tools import _data_admin, _private_values, data_admin, data_connect
from daimon.core.web_admin import WebAdmin
from daimon.data import identity, manage, redaction
from daimon.data.identity import NotAllowed
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

TENANT, ADMIN, MAYA, SOURCE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
KEYS = MultiFernet([Fernet(Fernet.generate_key())])
# What every change tool is called with, and the `daimon.data` call it must end in.
CHANGES: list[tuple[str, dict[str, Any], Any, str]] = [
    ("change_data_source", {"action": "pause", "source_id": str(uuid.uuid4())}, manage, "act"),
    ("change_data_access", {"action": "delete_scope", "scope_id": "ops"}, manage, "delete_scope"),
    (
        "change_data_access",
        {"action": "grant", "scope_id": "ops", "holder": "Maya"},
        manage,
        "grant",
    ),
    (
        "change_data_links",
        {"action": "unlink", "provider": "slack", "workspace": "T1", "subject": "U1"},
        identity,
        "unlink",
    ),
    (
        "change_data_links",
        {"action": "hide_names", "person": "Maya"},
        redaction,
        "set_redacted",
    ),
    ("approve_data_rule_set", {"name": "clients", "sha": "abc"}, redaction, "approve"),
]


class _Ctx:
    def __init__(self, auth: Any) -> None:
        self.auth = auth

    async def get_state(self, key: str) -> object:
        return self.auth if key == "auth" else None


def _schemas(_rt: object) -> Any:
    return "schemas"


def _keys(_rt: object) -> tuple[str, ...]:
    return ()


async def _tools(register: Callable[[FastMCP, Any], None], runtime: Any) -> dict[str, Any]:
    mcp = FastMCP("t")
    register(mcp, runtime)
    found: Sequence[Any] = await mcp.local_provider.list_tools()
    return {t.name: t for t in found}


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.role = "admin"
        self.cards: list[str] = []
        self.card_ok = True
        self.calls: list[tuple[str, list[Any]]] = []
        self.details = "direct_message"
        self.auth = make_auth()
        mp = monkeypatch

        async def on(*_: object) -> None:
            return None

        async def actor(*_: object) -> WebAdmin:
            return WebAdmin(tenant_id=TENANT, account_id=ADMIN, role=self.role)  # type: ignore[arg-type]

        async def card(_rt: object, _auth: object, *, tool_name: str, **_: object) -> None:
            self.cards.append(tool_name)
            if not self.card_ok:
                raise ToolError("Confirm this in your chat conversation. Nothing changed.")

        async def names(*_: object) -> dict[uuid.UUID, str]:
            return {MAYA: "Maya", ADMIN: "Ada"}

        async def details(*_: object) -> str:
            return self.details

        @asynccontextmanager
        async def store(*_: object) -> AsyncIterator[object]:
            yield object()

        mp.setattr(_data_admin, "data_is_on", on)
        mp.setattr(_data_admin, "web_actor_of", actor)
        mp.setattr(data_admin, "require_card", card)
        mp.setattr(data_admin, "names_of", names)
        mp.setattr(_private_values, "private_details", details)
        mp.setattr(data_admin, "store", store)
        mp.setattr(data_admin, "schemas_of", _schemas)
        mp.setattr(data_admin, "keys_of", _keys)
        for module, name in {(m, n) for _, _, m, n in CHANGES} | {
            (identity, "admin_link"),
            (manage, "create_source"),
        }:
            mp.setattr(module, name, self.record(name))

    def record(self, name: str) -> Callable[..., Any]:
        async def call(*args: Any, **kwargs: Any) -> redaction.SetInfo | None:
            self.calls.append((name, [*args, *kwargs.values()]))
            if name == "approve":
                return redaction.SetInfo(
                    "clients", None, None, None, "abc", "abc", False, True, None, None
                )

        return call

    async def call(self, tool: str, **args: Any) -> Any:
        runtime = SimpleNamespace(**vars(fake_runtime()), fernet=KEYS)
        tools = await _tools(data_admin.register_data_admin_tools, runtime)
        return await tools[tool].fn(ctx=_Ctx(self.auth), **args)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_data_sources", {}),
        ("list_data_access", {}),
        ("list_data_links", {}),
        ("list_data_rule_sets", {}),
        *[(t, {**a, "origin_context_id": "o"}) for t, a, _, _ in CHANGES],
    ],
)
async def test_a_member_is_refused_every_admin_tool(
    rig: Rig, tool: str, args: dict[str, Any]
) -> None:
    rig.role = "member"
    with pytest.raises(ToolError, match="Only an admin or owner"):
        await rig.call(tool, **args)
    assert not rig.calls and not rig.cards, "a refused caller reaches neither card nor store"


@pytest.mark.parametrize(("tool", "args", "_module", "function"), CHANGES)
async def test_a_change_runs_only_on_the_card(
    rig: Rig, tool: str, args: dict[str, Any], _module: Any, function: str
) -> None:
    rig.card_ok = False
    with pytest.raises(ToolError, match="Nothing changed"):
        await rig.call(tool, **args, origin_context_id="o")
    assert not rig.calls, "without the card nothing is written"
    rig.card_ok = True
    await rig.call(tool, **args, origin_context_id="o")
    assert rig.cards[-1] == tool and [c[0] for c in rig.calls] == [function], (
        f"{tool} asks for its card, then calls {function}"
    )
    if args.get("person") or args.get("holder"):
        sent = rig.calls[0][1]
        assert MAYA in sent or f"account:{MAYA}" in sent, (
            "a person named in chat is the workspace's Maya"
        )


async def test_a_source_credential_goes_only_through_the_askers_private_form(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential in the tool's arguments is refused; a source that needs one is made paused
    and its form posted, and set_credential posts the form for an existing source."""
    with pytest.raises(ToolError, match="Never pass a credential"):
        await rig.call(
            "change_data_source", action="create", kind="postgres", name="Orders",
            settings={"dsn": "postgresql://u:p@db.example.com/app"}, origin_context_id="o",
        )  # fmt: skip
    assert not rig.calls, "a credential never goes through chat"
    posted: list[str] = []

    async def form(*_: object, name: str, **__: object) -> Any:
        posted.append(name)
        return SimpleNamespace(instruction="Point at the form.")

    async def listed(*_: object) -> list[Any]:
        return [SimpleNamespace(id=SOURCE, name="Orders", kind="postgres")]

    monkeypatch.setattr(data_admin, "request_data_source_credential", form)
    monkeypatch.setattr(manage, "sources", listed)
    made = await rig.call(
        "change_data_source", action="create", kind="postgres", name="Orders",
        origin_context_id="o",
    )  # fmt: skip
    assert rig.calls[0][0] == "create_source" and True in rig.calls[0][1], "made credential_later"
    again = await rig.call(
        "change_data_source", action="set_credential", source_id=str(SOURCE), origin_context_id="o"
    )
    assert posted == ["Orders", "Orders"] and made["credential_form"] == again["credential_form"]


async def test_a_refusal_of_the_store_reads_as_its_message(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_: object, **__: object) -> None:
        raise NotAllowed("Only a workspace admin does that.")

    monkeypatch.setattr(manage, "delete_scope", refuse)
    with pytest.raises(ToolError, match="Only a workspace admin does that. Nothing changed."):
        await rig.call(
            "change_data_access", action="delete_scope", scope_id="ops", origin_context_id="o"
        )


async def test_access_lists_holders_by_name(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scopes(*_: object) -> list[manage.ScopeView]:
        held = [
            manage.GrantView(f"account:{MAYA}", False, None),
            manage.GrantView("role:x", True, None),
        ]
        return [manage.ScopeView("ops", "internal", "Ops", "", False, held)]

    monkeypatch.setattr(manage, "scopes", scopes)
    out = await rig.call("list_data_access")
    assert [h["name"] for h in out["scopes"][0]["holders"]] == ["Maya", None]
    assert "data_admins" not in out, "workspace admins manage data access"


@pytest.mark.parametrize("details", ["direct_message", "shown"])
async def test_emails_show_only_in_a_direct_message(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, details: str
) -> None:
    """In a room an email is a placeholder that opens only for the admin who listed it."""

    async def found(*_: object, **__: object) -> list[manage.IdentityView]:
        return [
            manage.IdentityView(
                "slack", "T1", "U1", "maya", "m@x.example", True, None, None, None, True, False
            )
        ]

    async def none(*_: object) -> list[object]:
        return []

    monkeypatch.setattr(manage, "identities", found)
    monkeypatch.setattr(identity, "list_candidates", none)
    rig.details = details
    email = (await rig.call("list_data_links"))["accounts"][0]["email"]
    if details == "shown":
        assert email == "m@x.example", "a direct message shows it as it is"
        return
    assert "m@x.example" not in email
    opened, _ = _private_values.open_placeholders(f"Mail {email}", rig.auth, KEYS)
    assert opened == "Mail m@x.example", "it opens for the admin it was fetched for"
    other, n = _private_values.open_placeholders(email, make_auth(), KEYS)
    assert (other, n) == (_private_values.WITHHELD, 0), "and for no one else"


# ---- a person's own accounts -------------------------------------------------------------


async def test_a_sign_in_goes_to_direct_messages_not_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    cards: list[str] = []
    pending = uuid.uuid4()

    async def card(_rt: object, _auth: object, *, tool_name: str, **_: object) -> None:
        cards.append(tool_name)

    async def person(*_: object) -> WebAdmin:
        return WebAdmin(tenant_id=TENANT, account_id=ADMIN, role="member")

    async def dm(_rt: object, _auth: object, *, recipient_id: str, content: str) -> None:
        sent.append(f"{recipient_id}: {content}")

    async def in_a_room(*_: object) -> str:
        return "direct_message"

    class Started:
        flow, source, url, user_code = "device", "Shared drive", "https://x.example/d", "WXYZ-1"
        pending_id, expires_at = pending, None

    class Fake:
        async def start(self, *_: object, **__: object) -> Started:
            return Started()

    def _connector(*_: object) -> Fake:
        return Fake()

    @asynccontextmanager
    async def store(*_: object) -> AsyncIterator[object]:
        yield object()

    mp = monkeypatch
    mp.setattr(data_connect, "require_card", card)
    mp.setattr(data_connect, "person", person)
    mp.setattr(data_connect, "send_direct_message_impl", dm)
    mp.setattr(_private_values, "private_details", in_a_room)
    mp.setattr(data_connect, "store", store)
    mp.setattr(data_connect, "_connector", _connector)
    tools = await _tools(data_connect.register_data_connect_tools, SimpleNamespace(fernet=KEYS))
    asker = make_auth(admin=False)
    out = await tools["connect_my_data"].fn(
        ctx=_Ctx(asker), source_id=str(uuid.uuid4()), origin_context_id="o"
    )
    assert cards == ["connect_my_data"], "starting a sign-in asks for the card"
    assert "WXYZ-1" not in str(out) and out["pending_id"] == str(pending), "a room sees no code"
    send = tools["send_private_details"].fn
    note = f"Enter {out['code']} at {out['url']}"
    with pytest.raises(ToolError, match="none of your private details"):
        await send(ctx=_Ctx(make_auth(admin=False)), message=note)
    assert await send(ctx=_Ctx(asker), message=note) == {"sent": "direct_message", "opened": 2}
    assert sent == ["u1: Enter WXYZ-1 at https://x.example/d"], "only the asker's DM opens it"
