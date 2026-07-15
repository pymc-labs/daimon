from __future__ import annotations

from pathlib import Path

from daimon.core.defaults.loader import (
    load_agent_specs,
    load_environment_specs,
    load_skill_paths,
    load_skill_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULTS = REPO_ROOT / "defaults"


def test_defaults_agents_parse() -> None:
    specs = load_agent_specs(DEFAULTS / "agents")
    assert any(s.name == "daimon" for s in specs)
    assert not any(s.name == "dev_agent" for s in specs), (
        "dev_agent must not be auto-seeded — it lives in agents-optional/ as an opt-in"
    )


def test_defaults_ships_dev_agent_with_copilot_mcp() -> None:
    """The dev_agent seed must declare the GitHub Copilot MCP server + a matching
    mcp_toolset (or MA 400s) and run on claude-opus-4-8 (proven in spikes 033/034).
    Moved out of auto-seeded defaults/agents/ into defaults/agents-optional/ so it
    stops appearing in every customer guild."""
    specs = load_agent_specs(DEFAULTS / "agents-optional")
    dev = next((s for s in specs if s.name == "dev_agent"), None)
    assert dev is not None, "defaults/agents-optional/dev_agent.yaml must exist"
    assert dev.model == "claude-sonnet-4-6"

    servers = dev.mcp_servers or []
    github = next((s for s in servers if s.get("name") == "github"), None)
    assert github is not None, "dev_agent must declare a 'github' mcp_server"
    assert github.get("url") == "https://api.githubcopilot.com/mcp"
    assert github.get("type") == "url"

    tools = dev.tools or []
    assert any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "github" for t in tools
    ), "dev_agent must reference the github mcp_server via an mcp_toolset (else MA 400s)"
    assert any(t.get("type") == "agent_toolset_20260401" for t in tools), (
        "dev_agent needs the builtin agent toolset (bash/read/edit/...) for repo work"
    )


def test_defaults_environments_parse() -> None:
    specs = load_environment_specs(DEFAULTS / "environments")
    assert any(s.name == "default" for s in specs)


def test_defaults_skills_parse() -> None:
    dirs = load_skill_paths(DEFAULTS / "skills")
    names = {load_skill_spec(d)[0].name for d in dirs}
    assert "cli-auth" in names, (
        "defaults/ tree ships the cli-auth skill, which is attached to the "
        "daimon agent so it knows how to mint short-lived CLI tokens via "
        "daimon-mcp:get_cli_token."
    )


def test_defaults_agent_skill_references_resolve() -> None:
    agents = load_agent_specs(DEFAULTS / "agents")
    skill_names = {load_skill_spec(d)[0].name for d in load_skill_paths(DEFAULTS / "skills")}
    for agent in agents:
        for ref in agent.skills:
            if ref.type != "custom":
                continue
            assert ref.skill_id in skill_names, (
                f"agent {agent.name!r} references missing skill {ref.skill_id!r}"
            )
