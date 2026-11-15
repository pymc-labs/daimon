"""The tools that undo or change something run only on a person's card in chat."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.mcp.tools import _card
from daimon.adapters.mcp.tools import agent_coding_keys as coding_key_tool
from daimon.adapters.mcp.tools import allowances as allowance_tool
from daimon.adapters.mcp.tools import my_data as my_data_tool
from daimon.adapters.mcp.tools import web_invites as invite_tool
from daimon.adapters.mcp.tools import web_people as people_tool
from daimon.adapters.mcp.tools import web_workspace_settings as settings_tool
from daimon.core.confirmation import confirmed_tools_for_turn
from daimon.core.stores import web_people as web_people_store
from daimon.core.stores import web_roles as web_roles_store
from daimon.core.tool_safety import CARDED_TOOLS, CONFIRMED_DAIMON_TOOLS, ToolSafetyPolicy
from daimon.core.turn.unattended_capabilities import protected_tools
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

_TOOLS = {
    "remove_my_data",
    "revoke_web_link",
    "delete_allowance",
    "update_web_person",
    "set_agent_web_access",
    "change_workspace_settings",
    "change_setting",
    "revert_agent",
    "set_agent_helpers",
    "set_agent_owners",
    "restore_library_skill_version",
    "approve_server_skill_update",
}


def _origin(*, attended: bool = True) -> Any:
    return SimpleNamespace(attended=attended)


@pytest.fixture
def card(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"origin": _origin(), "gap": None, "asked": []}

    async def origin(*_a: Any, **_k: Any) -> Any:
        return state["origin"]

    async def gap(_rt: Any, _auth: Any, _origin: Any, *, tool_name: str) -> Any:
        state["asked"].append(tool_name)
        return state["gap"]

    monkeypatch.setattr(_card, "get_verified_origin", origin)
    monkeypatch.setattr(_card, "session_card_gap", gap)
    return state


def test_carded_tools_when_listed_then_session_asks_and_unattended_turns_refuse() -> None:
    assert set(CARDED_TOOLS) == _TOOLS, "every destructive web and my-data tool is carded"
    assert set(CARDED_TOOLS) <= set(CONFIRMED_DAIMON_TOOLS), "the safety policy asks on them"
    deps = SimpleNamespace(
        backend_selection_enabled=False,
        microsoft_send=False,
        tool_safety=ToolSafetyPolicy(),
    )
    assert confirmed_tools_for_turn(deps) >= _TOOLS, "the session holds each on always-ask"  # type: ignore[arg-type]
    assert protected_tools(ToolSafetyPolicy()) >= _TOOLS, "an unattended turn cannot run them"


async def test_require_card_when_a_person_confirmed_then_returns(card: dict[str, Any]) -> None:
    await _card.require_card(
        fake_runtime(), make_auth(admin=False, agent_id=None), tool_name="x", origin_context_id="o"
    )
    assert card["asked"] == ["x"], "the live session was checked for the card"


@pytest.mark.parametrize(
    ("tool", "refused"), [("add_server_skill", True), ("approve_server_skill_update", False)]
)
async def test_require_card_when_cards_are_off_then_only_always_carded_tools_get_one(
    card: dict[str, Any], tool: str, refused: bool
) -> None:
    call = _card.require_card(
        fake_runtime(tool_safety=False),
        make_auth(),
        tool_name=tool,
        origin_context_id="o",
    )
    if refused:
        with pytest.raises(ToolError, match="approval cards are turned off"):
            await call
        assert not card["asked"], "no card can show, so the session is never asked"
    else:
        await call
        assert card["asked"] == [tool]


@pytest.mark.parametrize(
    "extra",
    [
        {"routine_id": uuid.uuid4()},
        {"agent_id": uuid.uuid4()},
        {"no_confirmation_surface": True},
        {"foreign_home_id": "other-org"},
    ],
    ids=["routine", "agent_key", "no_surface", "external_agent"],
)
async def test_require_card_when_no_person_can_confirm_then_refused(
    card: dict[str, Any], extra: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="Nothing changed"):
        await _card.require_card(
            fake_runtime(), make_auth(**extra), tool_name="x", origin_context_id="o"
        )
    assert not card["asked"], "a caller who cannot confirm never reaches the session check"


@pytest.mark.parametrize(
    ("origin", "gap"),
    [(None, None), (_origin(attended=False), None), (_origin(), "gap")],
    ids=["no_origin", "unattended", "no_card_on_session"],
)
async def test_require_card_when_the_turn_has_no_card_then_refused(
    card: dict[str, Any], origin: Any, gap: Any
) -> None:
    card.update(origin=origin, gap=gap)
    with pytest.raises(ToolError, match="Nothing changed"):
        await _card.require_card(fake_runtime(), make_auth(), tool_name="x", origin_context_id="o")


async def test_destructive_tools_when_no_card_then_refused_before_any_change() -> None:
    runtime, auth = fake_runtime(), make_auth(admin=False)
    calls = [
        my_data_tool._forget_impl(runtime, auth, str(uuid.uuid4())),  # pyright: ignore[reportPrivateUsage]
        my_data_tool._disconnect_impl(  # pyright: ignore[reportPrivateUsage]
            runtime, auth, kind="slack", agent_id=None, key=None
        ),
        coding_key_tool.revoke_coding_key(runtime, auth, str(uuid.uuid4()), None, "ctx"),
        coding_key_tool.revoke_coding_key(runtime, auth, str(uuid.uuid4()), "agent-1", "ctx"),
    ]
    for call in calls:
        with pytest.raises(ToolError, match="Nothing changed"):
            await call


async def test_web_admin_tools_when_no_card_then_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_grant(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(web_roles_store, "get_workspace_role", no_grant)
    monkeypatch.setattr(web_people_store, "get_person_for_account", no_grant)
    runtime, auth = fake_runtime(), make_auth()
    calls = [
        invite_tool._revoke_impl(runtime, auth, "invite", str(uuid.uuid4()), "ctx"),  # pyright: ignore[reportPrivateUsage]
        invite_tool._revoke_impl(runtime, auth, "visitor_link", str(uuid.uuid4()), "ctx"),  # pyright: ignore[reportPrivateUsage]
        allowance_tool._delete_impl(runtime, auth, str(uuid.uuid4())),  # pyright: ignore[reportPrivateUsage]
        people_tool._update_impl(runtime, auth, str(uuid.uuid4()), "admin", None),  # pyright: ignore[reportPrivateUsage]
        people_tool._access_set_impl(runtime, auth, "a1", True, None, None),  # pyright: ignore[reportPrivateUsage]
        settings_tool._change_impl(  # pyright: ignore[reportPrivateUsage]
            runtime,
            auth,
            modules=None,
            default_agent=None,
            everyone_invokes=True,
            invoker_people=[],
            dm_enabled=None,
            dm_memory_read_only=None,
            origin_context_id="ctx",
        ),
    ]
    for call in calls:
        with pytest.raises(ToolError, match="Nothing changed"):
            await call


async def test_session_card_gap_when_the_origin_is_unattended_then_no_session_can_ask() -> None:
    from daimon.adapters.mcp.tools._session_gate import session_card_gap

    gap = await session_card_gap(
        fake_runtime(), make_auth(), _origin(attended=False), tool_name="revert_agent"
    )
    assert gap == "no_origin", "a provider turn nobody watches must get the unattended verdict"
