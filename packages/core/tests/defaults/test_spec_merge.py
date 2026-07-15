"""Pure unit tests for `daimon.core.defaults.spec_merge` (Bug #12).

These cover collision rules, ordering, and empty-input handling for the
three union helpers. Integration coverage lives in
`test_reconcile_agents.py::test_reconcile_agent_preserves_user_attached_mcp_on_update`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsSkillParams,
)
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_anthropic_skill import (
    BetaManagedAgentsAnthropicSkill,
)
from anthropic.types.beta.beta_managed_agents_custom_skill import (
    BetaManagedAgentsCustomSkill,
)
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_mcp_toolset import (
    BetaManagedAgentsMCPToolset,
)
from anthropic.types.beta.beta_managed_agents_mcp_toolset_default_config import (
    BetaManagedAgentsMCPToolsetDefaultConfig,
)
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core.defaults.spec_merge import (
    merge_mcp_servers_with_ma,
    merge_skills_with_ma,
    merge_tools_with_ma,
)


def _ma_agent(
    *,
    mcp_servers: list[BetaManagedAgentsMCPServerURLDefinition] | None = None,
    skills: list[BetaManagedAgentsAnthropicSkill | BetaManagedAgentsCustomSkill] | None = None,
    tools: list[BetaManagedAgentsMCPToolset] | None = None,
) -> BetaManagedAgentsAgent:
    now = datetime.now(UTC)
    return BetaManagedAgentsAgent(
        id="ag_x",
        type="agent",
        name="daimon",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={},
        mcp_servers=mcp_servers or [],
        skills=skills or [],
        tools=cast(list, tools or []),
        created_at=now,
        updated_at=now,
        archived_at=None,
    )


def test_merge_mcp_servers_preserves_ma_only_entry() -> None:
    spec = [
        cast(
            BetaManagedAgentsURLMCPServerParams,
            {"name": "daimon-mcp", "type": "url", "url": "https://d/mcp"},
        )
    ]
    ma = _ma_agent(
        mcp_servers=[
            BetaManagedAgentsMCPServerURLDefinition(
                name="context7", type="url", url="https://c/mcp"
            )
        ]
    )
    out = merge_mcp_servers_with_ma(spec, ma)
    assert out is not None
    names = [s.get("name") for s in out]
    assert names == ["daimon-mcp", "context7"], (
        "spec entries first, MA-only entries appended in MA order"
    )


def test_merge_mcp_servers_spec_wins_on_name_collision() -> None:
    spec = [
        cast(
            BetaManagedAgentsURLMCPServerParams,
            {"name": "daimon-mcp", "type": "url", "url": "https://new/mcp"},
        )
    ]
    ma = _ma_agent(
        mcp_servers=[
            BetaManagedAgentsMCPServerURLDefinition(
                name="daimon-mcp", type="url", url="https://stale/mcp"
            )
        ]
    )
    out = merge_mcp_servers_with_ma(spec, ma)
    assert out is not None
    assert len(out) == 1, "name collision must not duplicate"
    assert out[0]["url"] == "https://new/mcp", "spec URL wins on name collision"


def test_merge_mcp_servers_returns_spec_unchanged_when_no_extras() -> None:
    spec = [
        cast(
            BetaManagedAgentsURLMCPServerParams,
            {"name": "daimon-mcp", "type": "url", "url": "https://d/mcp"},
        )
    ]
    ma = _ma_agent(mcp_servers=[])
    out = merge_mcp_servers_with_ma(spec, ma)
    assert out is spec, "no MA extras → return spec unchanged (no churn)"


def test_merge_skills_preserves_ma_only_entry() -> None:
    spec: list[BetaManagedAgentsSkillParams] = [
        cast(BetaManagedAgentsSkillParams, {"skill_id": "sk_spec", "type": "custom"})
    ]
    ma = _ma_agent(
        skills=[BetaManagedAgentsCustomSkill(skill_id="sk_user", type="custom", version="1")]
    )
    out = merge_skills_with_ma(spec, ma)
    ids = [s.get("skill_id") for s in out]
    assert ids == ["sk_spec", "sk_user"], "user-pinned skill must survive merge"


def test_merge_skills_spec_wins_on_collision() -> None:
    spec: list[BetaManagedAgentsSkillParams] = [
        cast(BetaManagedAgentsSkillParams, {"skill_id": "sk_same", "type": "custom"})
    ]
    ma = _ma_agent(
        skills=[BetaManagedAgentsCustomSkill(skill_id="sk_same", type="custom", version="1")]
    )
    out = merge_skills_with_ma(spec, ma)
    assert len(out) == 1, "skill_id collision must not duplicate"


def test_merge_tools_only_preserves_mcp_toolset_for_preserved_servers() -> None:
    spec_tools: list[Tool] = [cast(Tool, {"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"})]
    ma = _ma_agent(
        tools=[
            BetaManagedAgentsMCPToolset(
                type="mcp_toolset",
                mcp_server_name="context7",
                configs=[],
                default_config=BetaManagedAgentsMCPToolsetDefaultConfig.model_validate(
                    {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ),
            ),
            BetaManagedAgentsMCPToolset(
                type="mcp_toolset",
                mcp_server_name="orphan",  # not in preserved set → must NOT be added
                configs=[],
                default_config=BetaManagedAgentsMCPToolsetDefaultConfig.model_validate(
                    {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ),
            ),
        ]
    )
    out = merge_tools_with_ma(spec_tools, ma, preserved_mcp_names={"context7"})
    assert out is not None
    types_names = [(t.get("type"), t.get("mcp_server_name")) for t in out]
    assert ("mcp_toolset", "daimon-mcp") in types_names, "spec entries must survive"
    assert ("mcp_toolset", "context7") in types_names, (
        "mcp_toolset for preserved server must be appended"
    )
    assert ("mcp_toolset", "orphan") not in types_names, (
        "mcp_toolset for non-preserved server must be filtered"
    )


def test_merge_tools_does_not_duplicate_when_spec_already_has_toolset() -> None:
    spec_tools: list[Tool] = [cast(Tool, {"type": "mcp_toolset", "mcp_server_name": "context7"})]
    ma = _ma_agent(
        tools=[
            BetaManagedAgentsMCPToolset(
                type="mcp_toolset",
                mcp_server_name="context7",
                configs=[],
                default_config=BetaManagedAgentsMCPToolsetDefaultConfig.model_validate(
                    {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ),
            )
        ]
    )
    out = merge_tools_with_ma(spec_tools, ma, preserved_mcp_names={"context7"})
    assert out is spec_tools, "no extras → return spec unchanged"
