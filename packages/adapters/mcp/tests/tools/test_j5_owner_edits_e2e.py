"""J5 offline E2E: the owner (not a server admin) changes model/tools/skills, and it sticks.

A member in a fresh channel creates an agent from chat, so they own it. They change
its model, add a tool and attach a skill through the same chat tools, and every
later read (the next message's view of the agent) shows the change. They can also
preview a switch to another provider. Nothing here is an admin action. The fresh-chat
harness is J2's, as a member, and its fake Managed Agents also applies agent updates.

A gap on main is a strict xfail naming its owner (lanes/V2/JOURNEY-GAPS.md).
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.tools.agents import (
    _builtin_enabled,  # pyright: ignore[reportPrivateUsage]
    _create_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _get_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _list_agents_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
    _set_agent_builtin_tool_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.specs import AgentSpec
from mux.testing.drivers.anthropic.ma import list_response

from .test_j2_gemini_root_e2e import fresh_chat as fresh_chat

NAME = "carlos-notes"
SKILL_ID = "skill_notes"


@pytest.fixture
async def owner(fresh_chat):
    """J2's fresh chat, as a member; agent updates apply, and one tenant skill exists."""
    ma = fresh_chat.ma

    def update(call: httpx.Request, match: re.Match[str]) -> httpx.Response:
        stored = ma.agents.get(match.group(1))
        if stored is None:
            return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error"}})
        payload = json.loads(call.content)
        payload.pop("version", None)
        if isinstance(payload.get("model"), str):
            payload["model"] = {"id": payload["model"], "speed": "standard"}
        # Managed Agents reads an attached skill back pinned to its current version.
        for skill in payload.get("skills") or ():
            skill.setdefault("version", "1")
        stored.update(payload)
        stored["version"] = int(stored.get("version") or 1) + 1
        return httpx.Response(200, json=stored)

    skill = SkillListResponse(
        id=SKILL_ID,
        type="custom",
        display_title=tenant_scoped_display_title(
            tenant_id=fresh_chat.auth.tenant_id, name="notes"
        ),
        latest_version="1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")

    def create(call: httpx.Request, match: re.Match[str]) -> httpx.Response:
        # Managed Agents answers a model id with its model object, as it reads back.
        created = ma._create(call, match)  # pyright: ignore[reportPrivateUsage]
        stored = ma.agents[json.loads(created.content)["id"]]
        if isinstance(stored.get("model"), str):
            stored["model"] = {"id": stored["model"], "speed": "standard"}
        return httpx.Response(200, json=stored)

    fresh_chat.trace.router.routes[:0] = [
        ("POST", re.compile(r"/v1/agents$"), create),
        ("POST", re.compile(r"/v1/agents/([^/]+)$"), update),
        ("GET", re.compile(r"/v1/skills$"), lambda call, match: list_response([skill])),
    ]
    assert not fresh_chat.auth.is_admin
    return fresh_chat


async def _created(chat):
    info = await _create_agent_impl(
        chat.runtime,
        chat.auth,
        AgentSpec(name=NAME, model="claude-sonnet-4-6", system="You keep my notes."),
        origin_context_id=str(chat.origin.id),
    )
    return info


async def _read(chat, info):
    return await _get_agent_impl(
        chat.runtime,
        chat.auth,
        NAME,
        expected_ma_agent_id=info.id,
        origin_context_id=str(chat.origin.id),
    )


# The member variant only: an admin would prove nothing here (and J2's member xfail
# is about fork_agent, which J5 never calls).
MEMBER = pytest.mark.parametrize("fresh_chat", ["member"], indirect=True)


@MEMBER
async def test_j5_a_member_creates_an_agent_and_is_its_owner(owner):
    info = await _created(owner)
    seen = await _read(owner, info)
    assert seen.owner == "you"


@MEMBER
async def test_j5_the_owner_changes_its_model_and_it_sticks(owner):
    info = await _created(owner)
    changed = await _update_agent_impl(
        owner.runtime,
        owner.auth,
        NAME,
        model="opus 5.5",
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
        expected_ma_agent_id=info.id,
        origin_context_id=str(owner.origin.id),
    )
    assert changed.model == "claude-opus-5-5"
    assert (await _read(owner, info)).model == "claude-opus-5-5"


@MEMBER
async def test_j5_the_owner_adds_a_tool_and_it_sticks(owner):
    info = await _created(owner)
    await _update_agent_impl(
        owner.runtime,
        owner.auth,
        NAME,
        model=None,
        description=None,
        system=None,
        tools=[
            {
                "type": "custom",
                "name": "lookup_note",
                "description": "Look up a saved note by title.",
                "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
            }
        ],
        mcp_servers=None,
        skills=None,
        expected_ma_agent_id=info.id,
        origin_context_id=str(owner.origin.id),
    )
    stored = owner.ma.agents[info.id]
    assert any(tool.get("name") == "lookup_note" for tool in stored["tools"]), stored["tools"]


@MEMBER
async def test_j5_the_owner_attaches_a_skill_and_it_sticks(owner):
    info = await _created(owner)
    await _update_agent_impl(
        owner.runtime,
        owner.auth,
        NAME,
        model=None,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=["notes"],
        expected_ma_agent_id=info.id,
        origin_context_id=str(owner.origin.id),
    )
    assert SKILL_ID in [skill.skill_id for skill in (await _read(owner, info)).skills]


@MEMBER
async def test_j5_the_owner_previews_a_switch_to_gpt(owner):
    info = await _created(owner)
    preview = await _request_agent_backend_switch_impl(
        owner.runtime,
        owner.auth,
        agent_name=info.name,
        target_backend="openai",
        origin_context_id=str(owner.origin.id),
    )
    assert preview.status == "preview" and preview.target_backend == "openai"


@MEMBER
async def test_j5_the_create_result_says_you_own_it(owner):
    """SMOKE 0eee1b8e: the create result didn't say whether the creator owns the agent."""
    info = await _created(owner)
    assert info.owned_by_you is True and info.owner == "you"


@MEMBER
async def test_j5_the_owner_switches_to_gpt_and_the_read_back_says_so(owner):
    """SMOKE 0eee1b8e: after the switch the read-back still showed the Claude model."""
    from mux.profiles import backend_selection

    info = await _created(owner)
    switch = {
        "agent_name": info.name,
        "target_backend": "openai",
        "origin_context_id": str(owner.origin.id),
    }
    preview = await _request_agent_backend_switch_impl(owner.runtime, owner.auth, **switch)
    await _request_agent_backend_switch_impl(
        owner.runtime,
        owner.auth,
        **switch,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    seen = await _read(owner, info)
    assert seen.provider == "openai"
    assert seen.active_model == backend_selection("openai").model
    assert seen.model == "claude-sonnet-4-6", "its own model, the one update_agent changes"
    listed = await _list_agents_impl(owner.runtime, owner.auth, None, str(owner.origin.id))
    (mine,) = [agent for agent in listed if agent.id == info.id]
    assert (mine.provider, mine.active_model) == ("openai", seen.active_model)
    # The read-back's model round-trips into update_agent.
    await _update_agent_impl(
        owner.runtime,
        owner.auth,
        NAME,
        model=seen.model,
        description=None,
        system=None,
        tools=None,
        mcp_servers=None,
        skills=None,
        expected_ma_agent_id=info.id,
        origin_context_id=str(owner.origin.id),
    )


async def _builtin(chat, info, tool, enabled, *, auth=None):
    return await _set_agent_builtin_tool_impl(
        chat.runtime,
        auth or chat.auth,
        NAME,
        tool=tool,
        enabled=enabled,
        expected_ma_agent_id=info.id,
        origin_context_id=str(chat.origin.id),
    )


def _web_search_on(chat, info) -> bool | None:
    return _builtin_enabled(chat.ma.agents[info.id]["tools"], "web_search")


@MEMBER
async def test_j5_the_owner_turns_web_search_on_and_off_and_it_sticks(owner):
    """SMOKE 0eee1b8e: there was no tool to turn a built-in tool off."""
    info = await _created(owner)
    assert _web_search_on(owner, info) is True, "a new agent's toolset has it on"
    off = await _builtin(owner, info, "web_search", False)
    assert _web_search_on(owner, info) is False and "now off" in (off.applies or "")
    on = await _builtin(owner, info, "web_search", True)
    assert _web_search_on(owner, info) is True and "now on" in (on.applies or "")
    again = await _builtin(owner, info, "web_search", True)
    assert "already on" in (again.applies or "")


@MEMBER
async def test_j5_only_builtin_slash_name_in_denied_forbids_a_built_in(owner):
    """`builtin/web_search` in the operator's denied list forbids it; a bare name doesn't."""

    from fastmcp.exceptions import ToolError

    info = await _created(owner)

    def denying(*names: str):
        return _denying(owner, *names)

    await _builtin(owner, info, "web_search", False)
    await _builtin(denying("web_search"), info, "web_search", True)
    assert _web_search_on(owner, info) is True, "a bare name may be an MCP tool's"
    await _builtin(owner, info, "web_search", False)
    with pytest.raises(ToolError, match="turned web_search off for every agent"):
        await _builtin(denying("builtin/web_search"), info, "web_search", True)
    assert _web_search_on(owner, info) is False


@MEMBER
async def test_j5_another_member_cannot_change_the_owners_agent(owner):
    from dataclasses import replace

    from daimon.core.stores.tenants import get_tenant
    from daimon.testing.factories import make_account
    from fastmcp.exceptions import ToolError

    info = await _created(owner)
    async with owner.runtime.session_factory.begin() as session:
        tenant = await get_tenant(session, owner.auth.tenant_id)
        other = await make_account(session, tenant=tenant)
    stranger = replace(owner.auth, account_id=other.id, platform_user_id="999000999")
    with pytest.raises(ToolError):
        await _builtin(owner, info, "web_search", False, auth=stranger)
    assert _web_search_on(owner, info) is True


def _denying(chat, *names: str):
    from dataclasses import replace

    settings = chat.runtime.settings
    policy = settings.tool_safety.model_copy(update={"denied": names})
    return replace(
        chat,
        runtime=replace(chat.runtime, settings=settings.model_copy(update={"tool_safety": policy})),
    )


def _toolset(*configs: dict) -> list[dict]:
    return [{"type": "agent_toolset_20260401", "configs": list(configs)}]


@MEMBER
async def test_j5_update_agent_cannot_turn_on_a_forbidden_built_in(owner):
    from fastmcp.exceptions import ToolError

    info = await _created(owner)
    await _builtin(owner, info, "web_search", False)
    with pytest.raises(ToolError, match="turned web_search off for every agent"):
        await _update_agent_impl(
            _denying(owner, "builtin/web_search").runtime,
            owner.auth,
            NAME,
            model=None,
            description=None,
            system=None,
            tools=_toolset({"name": "web_search", "enabled": True}),
            mcp_servers=None,
            skills=None,
            expected_ma_agent_id=info.id,
            origin_context_id=str(owner.origin.id),
        )
    assert _web_search_on(owner, info) is False


@MEMBER
async def test_j5_a_new_agent_starts_with_a_forbidden_built_in_off(owner):
    from fastmcp.exceptions import ToolError

    denied = _denying(owner, "builtin/web_search")
    info = await _created(denied)
    assert _web_search_on(owner, info) is False, "on only by default, so written as off"
    with pytest.raises(ToolError, match="turned web_search off for every agent"):
        await _create_agent_impl(
            denied.runtime,
            owner.auth,
            AgentSpec(
                name="asks-for-search",
                model="claude-sonnet-4-6",
                tools=_toolset({"name": "web_search", "enabled": True}),
            ),
            origin_context_id=str(owner.origin.id),
        )


@MEMBER
async def test_j5_turning_on_one_built_in_adds_only_that_one(owner):
    info = await _created(owner)
    owner.ma.agents[info.id]["tools"] = [
        tool
        for tool in owner.ma.agents[info.id]["tools"]
        if not tool["type"].startswith("agent_toolset")
    ]
    await _builtin(owner, info, "web_search", True)
    stored = owner.ma.agents[info.id]["tools"]
    assert _web_search_on(owner, info) is True
    assert _builtin_enabled(stored, "bash") is False, "the defaults are not added"


@MEMBER
async def test_j5_an_ask_first_setting_survives_turning_a_tool_off_and_on(owner):
    info = await _created(owner)
    for tool in owner.ma.agents[info.id]["tools"]:
        if tool["type"].startswith("agent_toolset"):
            tool["configs"] = [
                *[c for c in tool.get("configs") or () if c.get("name") != "web_search"],
                {"name": "web_search", "permission_policy": {"type": "always_ask"}},
            ]
    await _builtin(owner, info, "web_search", False)
    await _builtin(owner, info, "web_search", True)
    (toolset,) = [
        t for t in owner.ma.agents[info.id]["tools"] if t["type"].startswith("agent_toolset")
    ]
    (config,) = [c for c in toolset["configs"] if c["name"] == "web_search"]
    assert config["permission_policy"] == {"type": "always_ask"} and config["enabled"] is True


@MEMBER
async def test_j5_an_admin_may_change_an_owners_built_in_but_not_a_system_agents(owner):
    from dataclasses import replace

    from daimon.core.stores.domain import Role
    from fastmcp.exceptions import ToolError

    info = await _created(owner)
    admin = replace(owner.auth, is_admin=True, role=Role.ADMIN)
    await _builtin(owner, info, "web_search", False, auth=admin)
    assert _web_search_on(owner, info) is False
    with pytest.raises(ToolError):
        await _set_agent_builtin_tool_impl(
            owner.runtime,
            admin,
            "Daimon",
            tool="web_search",
            enabled=False,
            expected_ma_agent_id="agent_daimon",
            origin_context_id=str(owner.origin.id),
        )


@MEMBER
async def test_j5_a_self_hosted_server_cannot_turn_a_built_in_on(owner):
    from dataclasses import replace

    from fastmcp.exceptions import ToolError

    info = await _created(owner)
    await _builtin(owner, info, "web_search", False)
    with pytest.raises(ToolError, match="runs its own model"):
        await _builtin(
            owner, info, "web_search", True, auth=replace(owner.auth, anthropic_disabled=True)
        )
    assert _web_search_on(owner, info) is False


@MEMBER
async def test_j5_a_skill_cannot_bring_in_a_forbidden_built_in(owner):
    """Attaching a skill to an agent with no toolset adds the base one: checked too."""
    from fastmcp.exceptions import ToolError

    info = await _created(owner)
    owner.ma.agents[info.id]["tools"] = [
        tool
        for tool in owner.ma.agents[info.id]["tools"]
        if not tool["type"].startswith("agent_toolset")
    ]
    with pytest.raises(ToolError, match="turned bash off for every agent"):
        await _update_agent_impl(
            _denying(owner, "builtin/bash").runtime,
            owner.auth,
            NAME,
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=["notes"],
            expected_ma_agent_id=info.id,
            origin_context_id=str(owner.origin.id),
        )
    assert _builtin_enabled(owner.ma.agents[info.id]["tools"], "bash") is None


def test_j5_a_built_in_on_in_any_toolset_is_on():
    off = {"type": "agent_toolset_20260401", "default_config": {"enabled": False}, "configs": []}
    on = {"type": "agent_toolset_20250522", "configs": [{"name": "bash", "enabled": True}]}
    assert _builtin_enabled([off], "bash") is False
    assert _builtin_enabled([off, on], "bash") is True
    assert _builtin_enabled([on, off], "bash") is True
    assert _builtin_enabled([{"type": "mcp_toolset", "mcp_server_name": "x"}], "bash") is None
