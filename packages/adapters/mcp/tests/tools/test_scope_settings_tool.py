"""`change_setting` and `get_settings`: the place is the verified turn, and every bypass is refused.

One test per bypass in the design: a non-admin, an agent key, a routine, an unattended
origin, a foreign origin, a turn with no card, admin standing lost between the card and
the write, overrides switched off, the cost footer at channel level, and a Daimon-granted
admin who may set the cost footer but not approvals.
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
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

ADMIN = Subject(is_admin=True, platform_user_id="u1")
MEMBER = Subject(platform_user_id="u1")


def _origin(*, attended: bool = True, platform: str = "discord") -> Any:
    return SimpleNamespace(
        attended=attended, platform=platform, parent_channel_id="C1", thread_id="T1"
    )


def _runtime(*, overrides: bool = True) -> Any:
    runtime = fake_runtime()
    settings = MagicMock()
    settings.tool_safety = ToolSafetyPolicy(enabled=True, allow_gate_overrides=overrides)
    settings.discord = object()
    settings.teams = None
    settings.thread_participation.mode = CascadeMode.OFF
    settings.progress_plans.mode = CascadeMode.OFF
    runtime.settings = settings  # type: ignore[misc]
    return runtime


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "origin": _origin(),
        "admin": [ADMIN],
        "card": AsyncMock(),
    }

    async def origin(*_a: Any, **_k: Any) -> Any:
        return state["origin"]

    async def native(*_a: Any, **_k: Any) -> Subject:
        answers: list[Subject] = state["admin"]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    fakes: dict[str, Any] = {
        "approval_admin": AsyncMock(side_effect=native),
        "set_approval_gate": AsyncMock(
            return_value=ApprovalGate("off", "server", "u1", None, "inherit")
        ),
        "set_switch": AsyncMock(
            return_value=SwitchChange(
                None, CascadeMode.ON, ResolvedCascade(CascadeMode.ON, "thread")
            )
        ),
        "get_cost_footer": AsyncMock(return_value=True),
        "set_cost_footer": AsyncMock(),
        "record_change": AsyncMock(),
        "load_access_policy": AsyncMock(return_value=TenantAccessPolicy()),
        "get_switch_modes": AsyncMock(return_value=CascadeModes(None, None, None)),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(core, name, fake)
    monkeypatch.setattr(tool, "get_verified_origin", origin)
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
        runtime or _runtime(),
        auth,
        key=key,
        level=level,
        value=value,
        origin_context_id=str(uuid.uuid4()),
    )


def _nothing_written(world: dict[str, Any]) -> None:
    world["set_approval_gate"].assert_not_awaited()
    world["set_switch"].assert_not_awaited()
    world["set_cost_footer"].assert_not_awaited()


async def test_native_admin_changes_approvals_after_the_card(world: dict[str, Any]) -> None:
    result = await _change(make_auth())
    world["card"].assert_awaited_once()
    assert world["card"].await_args.kwargs["tool_name"] == "change_setting"
    call = world["set_approval_gate"].await_args.kwargs
    assert call["target"] == "C1", "the place is the verified origin, never an argument"
    assert call["record"].surface == "chat"
    assert result.after == "off" and "Allow writes" in result.note


async def test_non_admin_is_refused_before_the_card(world: dict[str, Any]) -> None:
    world["admin"] = [MEMBER]
    with pytest.raises(ToolError, match="Only a server admin can change approvals"):
        await _change(make_auth(admin=False))
    world["card"].assert_not_awaited()
    _nothing_written(world)


@pytest.mark.parametrize(
    "extra",
    [
        {"agent_id": uuid.uuid4()},
        {"routine_id": uuid.uuid4()},
        {"execution_id": uuid.uuid4()},
        {"no_confirmation_surface": True},
        {"foreign_home_id": "other-org"},
    ],
    ids=["agent_key", "routine", "execution", "no_card_surface", "client"],
)
async def test_callers_who_are_not_a_person_at_a_card_are_refused(
    world: dict[str, Any], extra: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="Nothing changed"):
        await _change(make_auth(**extra))
    world["approval_admin"].assert_not_awaited()
    _nothing_written(world)


async def test_routine_is_refused_even_with_its_creator_admin(world: dict[str, Any]) -> None:
    """A routine's token may attest its creator's admin live; the tool refuses it anyway."""
    auth = make_auth(routine_id=uuid.uuid4())
    assert auth.admin_for(None)
    with pytest.raises(ToolError):
        await _change(auth, "participation", "server", "on")
    _nothing_written(world)


async def test_unattended_origin_is_refused(world: dict[str, Any]) -> None:
    world["origin"] = _origin(attended=False)
    with pytest.raises(ToolError, match="Nothing changed"):
        await _change(make_auth())
    _nothing_written(world)


async def test_foreign_or_unverified_origin_is_refused(world: dict[str, Any]) -> None:
    world["origin"] = None
    with pytest.raises(ToolError, match="Nothing changed"):
        await _change(make_auth())
    _nothing_written(world)


async def test_no_card_on_the_turn_writes_nothing(world: dict[str, Any]) -> None:
    world["card"].side_effect = ToolError(
        "Confirm this in your chat conversation. Nothing changed."
    )
    with pytest.raises(ToolError, match="Nothing changed"):
        await _change(make_auth())
    _nothing_written(world)


async def test_admin_lost_between_card_and_write_is_refused(world: dict[str, Any]) -> None:
    world["admin"] = [ADMIN, MEMBER]
    with pytest.raises(ToolError, match="Only a server admin"):
        await _change(make_auth())
    world["card"].assert_awaited_once()
    assert world["approval_admin"].await_count == 2, "checked live again after the press"
    _nothing_written(world)


async def test_overrides_off_saves_but_says_it_is_ignored(world: dict[str, Any]) -> None:
    result = await _change(make_auth(), runtime=_runtime(overrides=False))
    assert result.note.endswith(core.OVERRIDES_OFF)
    world["set_approval_gate"].assert_awaited_once()


async def test_cost_footer_at_channel_level_is_refused(world: dict[str, Any]) -> None:
    with pytest.raises(ToolError, match="set for the whole server only"):
        await _change(make_auth(), "cost_footer", "channel", "hidden")
    world["card"].assert_not_awaited()
    _nothing_written(world)


async def test_daimon_granted_admin_sets_cost_footer_but_not_approvals(
    world: dict[str, Any],
) -> None:
    granted = replace(make_auth(admin=True), is_platform_admin=False)
    assert granted.is_admin and not granted.is_platform_admin
    world["admin"] = [MEMBER]
    with pytest.raises(ToolError, match="Only a server admin can change approvals"):
        await _change(granted)
    result = await _change(granted, "cost_footer", "server", "hidden")
    assert result.after == "hidden"
    world["set_cost_footer"].assert_awaited_once()


async def test_member_sets_a_thread_switch(world: dict[str, Any]) -> None:
    result = await _change(make_auth(admin=False), "participation", "thread", "on")
    target = world["set_switch"].await_args.kwargs["target"]
    assert target.scope_id == "T1" and target.channel_id == "C1"
    assert result.after == "on"


async def test_get_settings_reads_the_origin_place(world: dict[str, Any]) -> None:
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


async def test_tools_take_no_place_arguments() -> None:
    from fastmcp import FastMCP

    mcp = FastMCP("settings")
    tool.register_scope_settings_tools(mcp, _runtime())
    tools = {t.name: t for t in await mcp.local_provider.list_tools()}
    for name in ("get_settings", "change_setting"):
        params = set(tools[name].parameters["properties"])
        assert not params & {"channel_id", "thread_id", "server_id", "guild_id", "tenant_id"}
    assert set(tools["change_setting"].parameters["properties"]) == {
        "origin_context_id",
        "key",
        "level",
        "value",
    }
