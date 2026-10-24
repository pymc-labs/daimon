"""J6 offline E2E: skills, built-in tools and Daimon MCP on every backend's root.

In a never-set-up channel, the built-in Daimon (Anthropic) uses a skill, a built-in
tool (web search) and an MCP tool. On OpenAI and Gemini the same agent's copy, made
and switched from chat on the code defaults only, must keep the same three. What a
root keeps on another backend is exactly what its consented switch does not drop
(`declared_drops`), and its first turn runs with that target (J2's E2E proves the
Gemini turn carries the MCP server). So each leg asks the production switch preview
what the copy would lose.

A gap on main is a strict xfail naming its owner, so it flips the moment it is fixed
(lanes/V2/JOURNEY-GAPS.md). The fresh-chat harness is J2's.
"""

from __future__ import annotations

import pytest
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _get_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.task_continuity import (
    _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_backend import load_backend_agent

from .test_j2_gemini_root_e2e import fresh_chat as fresh_chat

#: A custom skill as the built-in Daimon ships it (defaults/agents/daimon.yaml).
SKILL = {"type": "custom", "skill_id": "skill_file_handling", "version": "1"}
#: The declared loss that means the root lost each capability.
LOSS = {"skill": "skills", "builtin": "tools", "mcp": "mcp"}


def _gap(reason: str):
    return pytest.mark.xfail(strict=True, reason=reason)


J6B = "J6 (b) skills/built-in tools off Anthropic: scope waits on ROOT/Carlos (J6-OPTIONS.md)"
LEGS = [
    pytest.param("openai", "mcp"),  # #1441: GPT roots keep Daimon MCP
    pytest.param("openai", "skill", marks=_gap(J6B)),
    pytest.param("openai", "builtin"),  # J6b PR1: bash in its hosted environment
    pytest.param("gemini", "mcp"),
    pytest.param("gemini", "skill", marks=_gap(J6B)),
    pytest.param("gemini", "builtin"),  # J6b PR1: code_execution
]


@pytest.fixture
async def chat(fresh_chat, monkeypatch):
    """J2's fresh chat; its built-in Daimon also carries a custom skill, as shipped."""
    import re

    from anthropic.types.beta import SkillListResponse
    from daimon.core.defaults.metadata import tenant_scoped_display_title
    from daimon.core.mcp_oauth import discovery
    from mux.testing.drivers.anthropic.ma import list_response

    fresh_chat.ma.agents["agent_daimon"]["skills"] = [SKILL]
    # The skill is this tenant's own, as Managed Agents lists it.
    row = SkillListResponse(
        id=SKILL["skill_id"],
        type="custom",
        display_title=tenant_scoped_display_title(
            tenant_id=fresh_chat.auth.tenant_id, name="file-handling"
        ),
        latest_version="1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    fresh_chat.trace.router.routes.insert(
        0, ("GET", re.compile(r"/v1/skills$"), lambda call, match: list_response([row]))
    )

    async def listed(_url: str, _token: str) -> frozenset[str]:
        return frozenset({"describe_agent", "hand_off_task", "request_agent_backend_switch"})

    # Daimon's MCP catalogue as the template capture reads it live.
    monkeypatch.setattr(discovery, "list_mcp_tools", listed)
    return fresh_chat


# Nothing here forks, so the member variant (J2's strict xfail on fork) doesn't apply.
@pytest.mark.parametrize("fresh_chat", ["admin"], indirect=True)
async def test_j6_the_built_in_daimon_has_a_skill_a_built_in_tool_and_daimon_mcp(chat):
    """On Anthropic, nothing is represented: the agent itself carries all three."""
    daimon = await load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name="Daimon",
    )
    from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE, MCPToolset, agent_tools

    native = daimon.source
    assert native is not None and not native.spec.declared_drops
    assert [skill.id for skill in native.spec.skills or ()] == [SKILL["skill_id"]]
    (toolset,) = [
        extension
        for extension in native.spec.extensions or ()
        if extension.namespace == AGENT_TOOLS_NAMESPACE
    ]
    configured = agent_tools(toolset)
    assert any(
        isinstance(tool, MCPToolset) and tool.mcp_server_name == "daimon-mcp"
        for tool in configured.tools
    )
    # web_search comes with the base agent toolset (enabled by default).
    assert any(tool.name == "web_search" for tool in configured.intent())


@pytest.mark.parametrize(("backend", "capability"), LEGS)
async def test_j6_a_copy_on_another_backend_keeps_it(chat, backend, capability):
    """A copy made from chat and switched on the code defaults keeps the capability."""
    copy = await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
    )
    # As the person's "yes" does: this thread moves to the copy, then it switches.
    await _hand_off_task_impl(
        chat.runtime, chat.auth, origin_context_id=str(chat.origin.id), agent_id=copy.id
    )
    preview = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend=backend,
        origin_context_id=str(chat.origin.id),
    )
    assert LOSS[capability] not in preview.declared_drops, preview.declared_drops
    if capability == "builtin":
        # Its shell/code execution is kept; web search is a named loss (unpriced on
        # OpenAI, grounding off on Gemini), said plainly on the card.
        assert "builtin_tool:web_search" in preview.declared_drops, preview.declared_drops


@pytest.mark.parametrize("fresh_chat", ["member"], indirect=True)
async def test_j6_a_members_copy_still_reports_daimon_mcp(chat):
    """SMOKE 0eee1b8e J6o/J6g: a member's own copy said "MCP servers: None".

    The copy keeps `daimon-mcp`; only its URL is setup detail a member may not
    read. The capability name must stay visible, or the owner cannot tell a
    working Daimon MCP from one the switch dropped.
    """
    copy = await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
        origin_context_id=str(chat.origin.id),
    )
    seen = await _get_agent_impl(
        chat.runtime,
        chat.auth,
        copy.name,
        expected_ma_agent_id=copy.id,
        origin_context_id=str(chat.origin.id),
    )
    assert seen.mcp_servers is None, "the server URL stays setup detail for a member"
    assert "daimon-mcp" in seen.mcp_server_names


@pytest.mark.parametrize("backend", ["openai", "gemini"])
@pytest.mark.parametrize("fresh_chat", ["admin"], indirect=True)
async def test_j6_the_preview_keeps_mcp_with_no_provider_key_in_this_process(
    chat, monkeypatch, backend
):
    """SMOKE 0eee1b8e J6o/J6g: the MCP server builds the preview, and it may not carry
    the chat host's provider key. That must not turn MCP into a declared drop.

    Found by dsh (nc-smoke-0eee1b8e-J5J6J7). Played in J1's order (#1463): the copy is
    made in this conversation, and its preview runs before the card hands it over.
    """
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.provider_keys import DEFAULT_KEY_ENV
    from daimon.core.stores.access_policy import set_access_policy

    for provider in ("openai", "gemini"):
        monkeypatch.delenv(DEFAULT_KEY_ENV[provider], raising=False)
    # The tenant explicitly allows the providers, as staging's does: its MCP process
    # has no channel key, yet the switch itself is allowed.
    async with chat.runtime.session_factory.begin() as db:
        await set_access_policy(
            db,
            tenant_id=chat.auth.tenant_id,
            policy=TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini")),
        )
    copy = await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
        origin_context_id=str(chat.origin.id),
    )
    preview = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend=backend,
        origin_context_id=str(chat.origin.id),
    )
    assert "mcp" not in preview.declared_drops, preview.declared_drops


@pytest.mark.parametrize("backend", ["openai", "gemini"])
async def test_j6_a_copy_made_here_keeps_daimon_mcp_in_j1_order(chat, backend):
    """J1's order (#1463): copy in this conversation, preview, then the card hands off.

    The preview precedes the hand-off, so the capture must still take this place's
    template for the new copy (smoke 0eee1b8e J6o: the GPT copy had no MCP).
    """
    copy = await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
        origin_context_id=str(chat.origin.id),
    )
    preview = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend=backend,
        origin_context_id=str(chat.origin.id),
    )
    assert "mcp" not in preview.declared_drops, preview.declared_drops
