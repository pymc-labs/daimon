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
    _create_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _get_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
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
