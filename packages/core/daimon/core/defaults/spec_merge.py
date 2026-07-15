"""Merge spec entries with the current MA state for the UPDATE branch.

The defaults pipeline owns what it seeds in `defaults/*.yaml`; users can
attach extras (e.g. `context7` MCP, `xlsx` skill) directly to a managed
agent via SDK. Before this module, `reconcile_agent` sent the YAML spec
as the full PATCH body, which MA treats as a replace — wiping user
additions on every `daimon defaults apply`.

These pure helpers union spec entries with MA's current state. Spec wins
on collision (daimon re-asserts its seeded entries; removing a bundled
item then running apply re-adds it — that's the point of "defaults"
being authoritative for what it seeds).
"""

from __future__ import annotations

from typing import cast

from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)


def merge_mcp_servers_with_ma(
    spec_servers: list[BetaManagedAgentsURLMCPServerParams] | None,
    ma_agent: BetaManagedAgentsAgent,
) -> list[BetaManagedAgentsURLMCPServerParams] | None:
    """Union spec mcp_servers with MA's current entries, keyed by `name`.

    - Spec wins on name collision (daimon re-asserts its seeded entries).
    - Spec entries first, then MA-only entries in MA order.
    - Returns `spec_servers` unchanged when nothing on MA needs preserving.
    """
    spec_list = list(spec_servers) if spec_servers else []
    spec_names = {s.get("name") for s in spec_list}
    extras: list[BetaManagedAgentsURLMCPServerParams] = []
    for entry in ma_agent.mcp_servers:
        if entry.name in spec_names:
            continue
        preserved = cast(
            BetaManagedAgentsURLMCPServerParams,
            {"name": entry.name, "type": entry.type, "url": entry.url},
        )
        extras.append(preserved)
    if not extras:
        return spec_servers
    return spec_list + extras


def merge_skills_with_ma(
    spec_skills: list[BetaManagedAgentsSkillParams],
    ma_agent: BetaManagedAgentsAgent,
) -> list[BetaManagedAgentsSkillParams]:
    """Union resolved spec skills with MA's current skills, keyed by `skill_id`.

    Spec wins on collision. Spec entries first, MA-only entries appended in
    MA order.
    """
    spec_ids = {s.get("skill_id") for s in spec_skills}
    extras: list[BetaManagedAgentsSkillParams] = []
    for entry in ma_agent.skills:
        if entry.skill_id in spec_ids:
            continue
        preserved = cast(
            BetaManagedAgentsSkillParams,
            {"skill_id": entry.skill_id, "type": entry.type},
        )
        extras.append(preserved)
    return list(spec_skills) + extras


def merge_tools_with_ma(
    spec_tools: list[Tool] | None,
    ma_agent: BetaManagedAgentsAgent,
    *,
    preserved_mcp_names: set[str],
) -> list[Tool] | None:
    """Union spec tools with MA's mcp_toolset entries for preserved MCP servers.

    Only `mcp_toolset` entries get preserved from MA — and only those whose
    `mcp_server_name` is in `preserved_mcp_names` (the MA mcp_servers we
    decided to keep). Other tool types (`agent_toolset_20260401`, `custom`)
    are spec-authoritative; YAML drives what's there.

    Spec entries that already cover a preserved server's `mcp_server_name`
    are not duplicated.
    """
    spec_list = list(spec_tools) if spec_tools else []
    spec_mcp_names: set[str] = set()
    for tool in spec_list:
        if tool.get("type") == "mcp_toolset":
            name = tool.get("mcp_server_name")
            if isinstance(name, str):
                spec_mcp_names.add(name)

    extras: list[Tool] = []
    for entry in ma_agent.tools:
        if entry.type != "mcp_toolset":
            continue
        mcp_name = entry.mcp_server_name
        if mcp_name not in preserved_mcp_names or mcp_name in spec_mcp_names:
            continue
        preserved = cast(Tool, {"type": "mcp_toolset", "mcp_server_name": mcp_name})
        extras.append(preserved)
    if not extras:
        return spec_tools
    return spec_list + extras
