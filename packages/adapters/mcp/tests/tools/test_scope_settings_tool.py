"""`change_setting` and `get_settings`: the place is the verified turn, and every bypass is refused.

One row per bypass: not a person at a card (agent key, routine, execution, client, no card
surface), an unattended or foreign origin, a turn with no card, a non-admin, admin lost
between the card and the write, and a Daimon-granted admin who may set the cost footer
but not approvals.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.mcp.tools import scope_settings as tool
from daimon.core import scope_settings as core
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.approval_gate import ApprovalGate
from daimon.core.authz import Subject
from daimon.core.channel_switches import SwitchChange
from daimon.core.scope_cascade import CascadeMode, CascadeModes, ResolvedCascade
from daimon.core.tool_safety import ToolSafetyPolicy
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

ADMIN, MEMBER = Subject(is_admin=True, platform_user_id="u1"), Subject(platform_user_id="u1")


def _runtime(*, overrides: bool = True) -> Any:
    runtime = fake_runtime()
    settings = MagicMock()
    settings.tool_safety = ToolSafetyPolicy(enabled=True, allow_gate_overrides=overrides)
    settings.teams = None
    settings.thread_participation.mode = settings.progress_plans.mode = CascadeMode.OFF
    runtime.settings = settings  # type: ignore[misc]
    return runtime


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "origin": SimpleNamespace(
            attended=True, platform="discord", parent_channel_id="C1", thread_id="T1"
        ),
        "admin": [ADMIN],
        "card": AsyncMock(),
    }

    async def native(*_a: Any, **_k: Any) -> Subject:
        answers: list[Subject] = state["admin"]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    on = ResolvedCascade(CascadeMode.ON, "thread")
    fakes: dict[str, Any] = {
        "approval_admin": AsyncMock(side_effect=native),
        "set_approval_gate": AsyncMock(return_value=ApprovalGate("off", "server", "u1", None, "inherit")),
        "set_switch": AsyncMock(return_value=SwitchChange(None, CascadeMode.ON, on)),
        "get_cost_footer": AsyncMock(return_value=True),
        "set_cost_footer": AsyncMock(),
        "record_change": AsyncMock(),
        "load_access_policy": AsyncMock(return_value=TenantAccessPolicy()),
        "get_switch_modes": AsyncMock(return_value=CascadeModes(None, None, None)),
    }  # fmt: skip
    for name, fake in fakes.items():
        monkeypatch.setattr(core, name, fake)
    monkeypatch.setattr(
        tool, "get_verified_origin", AsyncMock(side_effect=lambda *_: state["origin"])
    )
    monkeypatch.setattr(tool, "require_card", state["card"])
    state.update(fakes)
    return state


async def _change(
    auth: Any,
    key: str = "approvals",
    level: str = "channel",
    value: str = "off",
    *,
    runtime: Any = None,
) -> Any:
    return await tool._change_setting_impl(  # pyright: ignore[reportPrivateUsage]
        runtime or _runtime(), auth, key=key, level=level, value=value, origin_context_id=str(uuid.uuid4())
    )  # fmt: skip


def _granted() -> Any:
    """Admin in Daimon (every delegated action) but not an admin of the platform itself."""
    return replace(make_auth(admin=True), is_platform_admin=False)


# (case, auth, change(key, level, value), world setup, refusal match, card shown?)
BYPASSES: list[tuple[str, Any, tuple[str, str, str], dict[str, Any], str, bool]] = [
    ("agent_key", lambda: make_auth(agent_id=uuid.uuid4()), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("routine_with_admin_creator", lambda: make_auth(routine_id=uuid.uuid4()), ("participation", "server", "on"), {}, "Nothing changed", False),
    ("execution", lambda: make_auth(execution_id=uuid.uuid4()), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("no_card_surface", lambda: make_auth(no_confirmation_surface=True), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("client", lambda: make_auth(foreign_home_id="other-org"), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("unattended", make_auth, ("approvals", "channel", "off"), {"origin": SimpleNamespace(attended=False, platform="discord", parent_channel_id="C1", thread_id="T1")}, "Nothing changed", False),
    ("foreign_origin", make_auth, ("approvals", "channel", "off"), {"origin": None}, "Nothing changed", False),
    ("no_card", make_auth, ("approvals", "channel", "off"), {"card": AsyncMock(side_effect=ToolError("Confirm this in chat. Nothing changed."))}, "Nothing changed", True),
    ("non_admin", lambda: make_auth(admin=False), ("approvals", "channel", "off"), {"admin": [MEMBER]}, "Only a server admin can change approvals", False),
    ("admin_lost_after_press", make_auth, ("approvals", "channel", "off"), {"admin": [ADMIN, MEMBER]}, "Only a server admin", True),
    ("daimon_admin_vs_approvals", _granted, ("approvals", "channel", "off"), {"admin": [MEMBER]}, "Only a server admin can change approvals", False),
    ("footer_at_channel", make_auth, ("cost_footer", "channel", "hidden"), {}, "set for the whole server only", False),
]  # fmt: skip


@pytest.mark.parametrize(
    ("case", "auth", "change", "setup", "refused", "carded"), BYPASSES, ids=[b[0] for b in BYPASSES]
)
async def test_every_bypass_is_refused_and_writes_nothing(
    world: dict[str, Any], case: str, auth: Any, change: tuple[str, str, str], setup: dict[str, Any], refused: str, carded: bool
) -> None:  # fmt: skip
    if "card" in setup:
        world["card"].side_effect = setup.pop("card").side_effect
    world.update(setup)
    caller = auth()
    if case == "routine_with_admin_creator":
        assert caller.admin_for(None), "a routine's token may attest its creator's admin live"
    with pytest.raises(ToolError, match=refused):
        await _change(caller, *change)
    assert world["card"].await_count == carded
    for writer in ("set_approval_gate", "set_switch", "set_cost_footer"):
        world[writer].assert_not_awaited()
    if case == "admin_lost_after_press":
        assert world["approval_admin"].await_count == 2, "checked live again after the press"


async def test_allowed_changes_write_at_the_verified_origin(world: dict[str, Any]) -> None:
    approvals = await _change(make_auth())
    assert world["card"].await_args.kwargs["tool_name"] == "change_setting"
    call = world["set_approval_gate"].await_args.kwargs
    assert call["target"] == "C1" and call["record"].surface == "chat", (
        "the origin, not an argument"
    )
    assert approvals.after == "off" and "Allow writes" in approvals.note
    ignored = await _change(make_auth(), runtime=_runtime(overrides=False))
    assert ignored.note.endswith(core.OVERRIDES_OFF)
    assert (await _change(_granted(), "cost_footer", "server", "hidden")).after == "hidden"
    await _change(make_auth(admin=False), "participation", "thread", "on")
    target = world["set_switch"].await_args.kwargs["target"]
    assert (target.scope_id, target.channel_id) == ("T1", "C1")


async def test_get_settings_reads_the_origin_place_and_no_tool_names_a_place(
    world: dict[str, Any],
) -> None:
    result = await tool._get_settings_impl(_runtime(), make_auth(admin=False), "ctx")  # pyright: ignore[reportPrivateUsage]
    assert [s.key for s in result.settings] == [
        "approvals",
        "participation",
        "progress_plans",
        "cost_footer",
    ]
    assert [lv.level for lv in result.settings[0].levels] == ["server", "channel", "thread"]
    world["origin"] = None
    with pytest.raises(ToolError):
        await tool._get_settings_impl(_runtime(), make_auth(), "ctx")  # pyright: ignore[reportPrivateUsage]
    mcp = FastMCP("settings")
    tool.register_scope_settings_tools(mcp, _runtime())
    tools = {t.name: t for t in await mcp.local_provider.list_tools()}
    assert set(tools["change_setting"].parameters["properties"]) == {
        "origin_context_id",
        "key",
        "level",
        "value",
    }
    assert set(tools["get_settings"].parameters["properties"]) == {"origin_context_id"}
