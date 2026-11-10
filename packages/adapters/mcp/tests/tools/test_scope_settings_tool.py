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
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.tools import scope_settings as tool
from daimon.core.authz import Subject
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features import service
from daimon.core.features.registry import FEATURES, Tier
from daimon.core.features.surface import OVERRIDES_OFF
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.factories import make_account, make_tenant
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .parity_fakes import make_auth

ADMIN, MEMBER = Subject(is_admin=True, platform_user_id="u1"), Subject(platform_user_id="u1")
_SESSIONS: list[async_sessionmaker[AsyncSession]] = []


def _runtime(*, overrides: bool = True) -> Any:
    settings = Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "tool_safety": ToolSafetyPolicy(enabled=True, allow_gate_overrides=overrides),
        }
    )
    return SimpleNamespace(session_factory=_SESSIONS[0], client=object(), settings=settings)


@pytest.fixture
async def world(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> Any:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    _SESSIONS[:] = [db_session_factory]
    state: dict[str, Any] = {
        "origin": SimpleNamespace(
            attended=True,
            platform="discord",
            parent_channel_id="C1",
            thread_id="T1",
            responder_ma_agent_id="agent_1",
        ),
        "admin": [ADMIN],
        "card": AsyncMock(),
        "ids": {"tenant_id": tenant.id, "account_id": account.id},
    }

    async def native(*_a: Any, **_k: Any) -> Subject:
        answers: list[Subject] = state["admin"]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    state["approval_admin"] = AsyncMock(side_effect=native)
    monkeypatch.setattr(service, "approval_admin", state["approval_admin"])
    monkeypatch.setattr(
        tool, "get_verified_origin", AsyncMock(side_effect=lambda *_: state["origin"])
    )
    monkeypatch.setattr(tool, "require_card", state["card"])
    yield state
    _SESSIONS.clear()


def _as(world: dict[str, Any], auth: Any) -> Any:
    return replace(auth, **world["ids"])


async def _changes(world: dict[str, Any]) -> list[Any]:
    async with _SESSIONS[0]() as session:
        tenant = world["ids"]["tenant_id"]
        return [
            *await list_changes(session, tenant_id=tenant, channel_id="C1"),
            *await list_changes(session, tenant_id=tenant, channel_id=WORKSPACE_ID),
        ]


async def _change(
    auth: Any,
    key: str = "approvals",
    tier: Tier = "channel",
    value: str = "off",
    *,
    runtime: Any = None,
) -> Any:
    return await tool._change_setting_impl(  # pyright: ignore[reportPrivateUsage]
        runtime or _runtime(), auth, key=key, tier=tier, value=value, origin_context_id=str(uuid.uuid4())
    )  # fmt: skip


def _granted() -> Any:
    """Admin in Daimon (every delegated action) but not an admin of the platform itself."""
    return replace(make_auth(admin=True), is_platform_admin=False)


# (case, auth, change(key, tier, value), world setup, refusal match, card shown?)
BYPASSES: list[tuple[str, Any, tuple[str, Tier, str], dict[str, Any], str, bool]] = [
    ("agent_key", lambda: make_auth(agent_id=uuid.uuid4()), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("routine_with_admin_creator", lambda: make_auth(routine_id=uuid.uuid4()), ("participation", "workspace", "on"), {}, "Nothing changed", False),
    ("execution", lambda: make_auth(execution_id=uuid.uuid4()), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("no_card_surface", lambda: make_auth(no_confirmation_surface=True), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("client", lambda: make_auth(foreign_home_id="other-org"), ("approvals", "channel", "off"), {}, "Nothing changed", False),
    ("unattended", make_auth, ("approvals", "channel", "off"), {"origin": SimpleNamespace(attended=False, platform="discord", parent_channel_id="C1", thread_id="T1")}, "Nothing changed", False),
    ("foreign_origin", make_auth, ("approvals", "channel", "off"), {"origin": None}, "Nothing changed", False),
    ("no_card", make_auth, ("approvals", "channel", "off"), {"card": AsyncMock(side_effect=ToolError("Confirm this in chat. Nothing changed."))}, "Nothing changed", True),
    ("non_admin", lambda: make_auth(admin=False), ("approvals", "channel", "off"), {"admin": [MEMBER]}, "Only a server admin can change approvals", False),
    ("admin_lost_after_press", make_auth, ("approvals", "channel", "off"), {"admin": [ADMIN, MEMBER]}, "Only a server admin", True),
    ("daimon_admin_vs_approvals", _granted, ("approvals", "channel", "off"), {"admin": [MEMBER]}, "Only a server admin can change approvals", False),
    ("approvals_at_agent", make_auth, ("approvals", "agent", "off"), {}, "can't be set for the agent", False),
    ("guardrail_by_agent_key", lambda: make_auth(agent_id=uuid.uuid4()), ("unattended_capabilities", "workspace", "off"), {}, "Nothing changed", False),
    ("guardrail_unattended", make_auth, ("require_routine_destination", "workspace", "on"), {"origin": SimpleNamespace(attended=False, platform="discord", parent_channel_id="C1", thread_id="T1")}, "Nothing changed", False),
    ("guardrail_no_card", make_auth, ("reach_budget", "channel", "on"), {"card": AsyncMock(side_effect=ToolError("Confirm this in chat. Nothing changed."))}, "Nothing changed", True),
    ("guardrail_looser_below", make_auth, ("reach_budget", "workspace", "off"), {}, "can only make it stricter", False),
]  # fmt: skip


@pytest.mark.parametrize(
    ("case", "auth", "change", "setup", "refused", "carded"), BYPASSES, ids=[b[0] for b in BYPASSES]
)
async def test_every_bypass_is_refused_and_writes_nothing(
    world: dict[str, Any], case: str, auth: Any, change: tuple[str, Tier, str], setup: dict[str, Any], refused: str, carded: bool
) -> None:  # fmt: skip
    if "card" in setup:
        world["card"].side_effect = setup.pop("card").side_effect
    world.update(setup)
    caller = auth()
    if case == "routine_with_admin_creator":
        assert caller.admin_for(None), "a routine's token may attest its creator's admin live"
    with pytest.raises(ToolError, match=refused):
        await _change(_as(world, caller), *change)
    assert world["card"].await_count == carded
    assert await _changes(world) == []
    if case == "admin_lost_after_press":
        assert world["approval_admin"].await_count == 2, "checked live again after the press"


async def test_allowed_changes_write_at_the_verified_origin(world: dict[str, Any]) -> None:
    approvals = await _change(_as(world, make_auth()))
    assert world["card"].await_args.kwargs["tool_name"] == "change_setting"
    assert approvals.setting["value"] == "off" and "Allow writes" in approvals.note
    ignored = await _change(
        _as(world, make_auth()), "approvals", "channel", "on", runtime=_runtime(overrides=False)
    )
    assert ignored.note.endswith(OVERRIDES_OFF), ignored.note
    footer = await _change(_as(world, _granted()), "cost_footer", "workspace", "hidden")
    assert footer.setting["value"] == "hidden", footer.setting
    agent = await _change(_as(world, make_auth()), "progress_plans", "agent", "on")
    assert agent.setting["source"] == "agent", "the agent is the origin's responder"
    await _change(_as(world, make_auth(admin=False)), "participation", "thread", "on")
    changes = await _changes(world)
    recorded = {(c.setting, c.surface, str(c.after)) for c in changes}
    thread = [c.after for c in changes if c.setting == "thread_participation"]
    assert thread == [{"thread": "T1", "mode": "on"}]
    assert ("approvals", "chat", "off") in recorded, "the origin's channel, not an argument"


async def test_a_guardrail_change_waits_for_the_card(world: dict[str, Any]) -> None:
    guard = await _change(_as(world, make_auth()), "unattended_capabilities", "agent", "off")
    assert guard.setting["value"] == "off" and world["card"].await_count == 1, (
        "a guardrail asks on a card"
    )
    assert world["card"].await_args.kwargs["tool_name"] == "change_setting"
    assert "unattended_capabilities" in {c.setting for c in await _changes(world)}, "recorded"


async def test_get_settings_reads_the_origin_place_and_no_tool_names_a_place(
    world: dict[str, Any],
) -> None:
    result = await tool._get_settings_impl(_runtime(), _as(world, make_auth(admin=False)), "ctx")  # pyright: ignore[reportPrivateUsage]
    assert [s["key"] for s in result.settings] == list(FEATURES), "every registered key"
    by_key = {s["key"]: [t["tier"] for t in s["tiers"]] for s in result.settings}
    assert by_key["approvals"] == ["workspace", "channel", "thread"]
    assert by_key["progress_plans"] == ["deployment", "workspace", "agent", "channel", "thread"]
    world["origin"] = None
    with pytest.raises(ToolError):
        await tool._get_settings_impl(_runtime(), make_auth(), "ctx")  # pyright: ignore[reportPrivateUsage]
    mcp = FastMCP("settings")
    tool.register_scope_settings_tools(mcp, _runtime())
    tools = {t.name: t for t in await mcp.local_provider.list_tools()}
    assert set(tools["change_setting"].parameters["properties"]) == {
        "origin_context_id",
        "key",
        "tier",
        "value",
        "locked",
    }
    assert set(tools["get_settings"].parameters["properties"]) == {"origin_context_id"}
