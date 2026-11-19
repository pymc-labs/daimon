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
    _get_agent_impl,  # pyright: ignore[reportPrivateUsage]
)

from .test_j2_gemini_root_e2e import fresh_chat as fresh_chat

#: A custom skill as the built-in Daimon ships it (defaults/agents/default.yaml).
SKILL = {"type": "custom", "skill_id": "skill_file_handling", "version": "1"}
#: The declared loss that means the root lost each capability.
LOSS = {"skill": "skills", "builtin": "tools", "mcp": "mcp"}

LEGS = [
    pytest.param("openai", "mcp"),  # #1441: GPT roots keep Daimon MCP
    pytest.param("openai", "skill"),  # custom skills ride to OpenAI templates
    pytest.param("openai", "builtin"),  # J6b PR1: bash in its hosted environment
    pytest.param("gemini", "mcp"),
    pytest.param("gemini", "skill"),  # custom skills attach to Gemini sessions
    pytest.param("gemini", "builtin"),  # J6b PR1: code_execution
]


@pytest.fixture
async def chat(fresh_chat, monkeypatch):
    """J2's fresh chat; its built-in Daimon also carries a custom skill, as shipped."""
    import io
    import re
    import zipfile

    import httpx
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
    # Its archive, which the switch preview downloads once to size the skill.
    version = {"id": "skillver_file_handling_1", "version": SKILL["version"]}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as packed:
        packed.writestr(
            "file-handling/SKILL.md", "---\nname: file-handling\ndescription: Files.\n---\n"
        )
    base = rf"/v1/skills/{SKILL['skill_id']}/versions"
    fresh_chat.trace.router.routes[1:1] = [
        ("GET", re.compile(base + "$"), lambda call, match: list_response([version])),
        (
            "GET",
            re.compile(rf"{base}/{version['id']}/content$"),
            lambda call, match: httpx.Response(200, content=stream.getvalue()),
        ),
    ]

    async def listed(_url: str, _token: str) -> frozenset[str]:
        return frozenset({"describe_agent", "hand_off_task", "request_agent_backend_switch"})

    # Daimon's MCP catalogue as the template capture reads it live.
    monkeypatch.setattr(discovery, "list_mcp_tools", listed)
    return fresh_chat


# Nothing here forks, so the member variant adds nothing.


@pytest.mark.parametrize("fresh_chat", ["member"], indirect=True)
async def test_j6_a_member_never_sees_the_connectors_of_an_agent_they_dont_own(chat):
    """Only Daimon's own connection is named to a member; an admin's private one is not."""
    from daimon.adapters.mcp.tools.agents import (
        _list_agents_impl,  # pyright: ignore[reportPrivateUsage]
    )

    # The built-in Daimon (an admin's agent) also has a private connector.
    chat.ma.agents["agent_daimon"]["mcp_servers"] = [
        *chat.ma.agents["agent_daimon"]["mcp_servers"],
        {"name": "finance-crm", "type": "url", "url": "https://crm.invalid/mcp"},
    ]
    seen = await _get_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        origin_context_id=str(chat.origin.id),
    )
    assert seen.mcp_servers is None
    assert seen.mcp_server_names in ([], ["daimon-mcp"])
    listed = await _list_agents_impl(
        chat.runtime, chat.auth, None, origin_context_id=str(chat.origin.id)
    )
    for info in listed:
        assert "finance-crm" not in info.mcp_server_names, info.name
