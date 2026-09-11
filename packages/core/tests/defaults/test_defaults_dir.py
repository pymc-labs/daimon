from __future__ import annotations

import re
from pathlib import Path

from daimon.core.constants import DEFAULT_AGENT_MODEL
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
    mcp_toolset (or MA 400s) and run on the current-generation Sonnet.
    Moved out of auto-seeded defaults/agents/ into defaults/agents-optional/ so it
    stops appearing in every customer guild.

    The model is asserted against DEFAULT_AGENT_MODEL rather than a literal: this
    seed sat on claude-sonnet-4-6 for a generation after the shipped agent moved
    on, and a hardcoded expectation is what let that pass. The Sonnet family
    itself is deliberate — an earlier revision ran claude-opus-4-8 and was moved
    off it.
    """
    specs = load_agent_specs(DEFAULTS / "agents-optional")
    dev = next((s for s in specs if s.name == "dev_agent"), None)
    assert dev is not None, "defaults/agents-optional/dev_agent.yaml must exist"
    assert dev.model == DEFAULT_AGENT_MODEL, (
        f"dev_agent pins {dev.model}; expected the shared default {DEFAULT_AGENT_MODEL}"
    )

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


def test_defaults_daimon_agent_guidance_routes_credentials_to_request_tools() -> None:
    """Seeded guidance names callable setup tools and avoids obsolete panel paths."""
    specs = load_agent_specs(DEFAULTS / "agents")
    daimon = next(s for s in specs if s.name == "daimon")
    skill_names = {ref.skill_id for ref in daimon.skills if ref.type == "custom"}
    skill_bodies = [
        load_skill_spec(d)[1]
        for d in load_skill_paths(DEFAULTS / "skills")
        if load_skill_spec(d)[0].name in skill_names
    ]
    combined = "\n".join([daimon.system or "", *skill_bodies])
    assert "request_agent_key" in combined, (
        "guidance must name request_agent_key for ad hoc env secrets"
    )
    assert "request_mcp_token" in combined, (
        "guidance must name request_mcp_token for auth-required MCP servers"
    )
    retired_tools = (
        "request_env_credential",
        "request_mcp_credential",
        "request_skill_repo_credential",
        "list_env_credential_keys",
        "remove_env_credential",
        "skills_sync",
        "skills_list",
        "skills_get",
        "skills_delete",
    )
    for name in retired_tools:
        assert name not in combined, f"seeded guidance names a retired tool: {name}"
    for line in combined.splitlines():
        lowered = line.lower()
        assert not re.search(r"\bdoor\b|repo\+auth|repo-auth|mcps modal|\benv vars?\b", lowered), (
            f"seeded guidance contains an obsolete setup label: {line!r}"
        )
        if "/agent-setup" in lowered:
            assert not any(
                word in lowered for word in ("token", "secret", "credential", "modal", "→", "->")
            ), f"setup entry must not invent a nested path or route private input: {line!r}"


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
