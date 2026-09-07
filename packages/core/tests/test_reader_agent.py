from __future__ import annotations

from typing import Any

from daimon.core.reader_agent import READER_BLOCK, READER_SKILL_NAME, derive_reader_spec
from daimon.core.specs import AgentSpec, SkillRef


def _agent_spec(**overrides: object) -> AgentSpec:
    defaults: dict[str, object] = {
        "name": "alpha",
        "model": "claude-sonnet-5",
        "system": "You are alpha, a data analyst.",
    }
    defaults.update(overrides)
    return AgentSpec.model_validate(defaults)


def test_derive_reader_spec_appends_reader_suffix_to_name() -> None:
    source = _agent_spec(name="alpha")

    derived = derive_reader_spec(source)

    assert derived.name == "alpha-reader"


def test_derive_reader_spec_marks_isolated_with_no_mcp_servers() -> None:
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
    )

    derived = derive_reader_spec(source)

    assert derived.isolated is True, "reader variant must be created via create_isolated_session"
    assert derived.mcp_servers is None, "reader variant must have no MCP surface"


def test_derive_reader_spec_drops_only_mcp_toolset_tool() -> None:
    tools: list[dict[str, Any]] = [
        {"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"},
        {
            "type": "custom",
            "name": "lookup",
            "description": "Look something up.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=tools,
    )

    derived = derive_reader_spec(source)

    assert derived.tools is not None
    tool_types = [tool.get("type") for tool in derived.tools]
    assert tool_types == ["custom"], "only the mcp_toolset entry should be dropped"


def test_derive_reader_spec_collapses_tools_to_none_when_only_mcp_toolset_present() -> None:
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
    )

    derived = derive_reader_spec(source)

    assert derived.tools is None, (
        "an empty list and None are not equivalent downstream — must collapse to None"
    )


def test_derive_reader_spec_adds_report_reader_skill_alongside_existing_skills() -> None:
    source = _agent_spec(skills=[SkillRef(type="custom", skill_id="pymc-artifact-style")])

    derived = derive_reader_spec(source)

    skill_ids = [ref.skill_id for ref in derived.skills]
    assert skill_ids == ["pymc-artifact-style", READER_SKILL_NAME]


def test_derive_reader_spec_is_idempotent_on_skills_and_system() -> None:
    source = _agent_spec(skills=[SkillRef(type="custom", skill_id="pymc-artifact-style")])

    once = derive_reader_spec(source)
    twice = derive_reader_spec(once)

    reader_skill_count = sum(1 for ref in twice.skills if ref.skill_id == READER_SKILL_NAME)
    assert reader_skill_count == 1, "deriving twice must not duplicate the report-reader skill ref"
    assert twice.system is not None
    assert twice.system.count(READER_BLOCK) == 1, (
        "deriving twice must not duplicate the appended reader block"
    )


def test_derive_reader_spec_appends_reader_block_to_source_system() -> None:
    source = _agent_spec(system="You are alpha, a data analyst.")

    derived = derive_reader_spec(source)

    assert derived.system is not None
    assert derived.system.startswith(source.system or "")
    assert "cite the file and the column" in derived.system, (
        "reader block must carry the citation clause"
    )


def test_derive_reader_spec_does_not_mutate_source() -> None:
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
    )

    derive_reader_spec(source)

    assert source.mcp_servers is not None, "source spec must be untouched by derivation"
    assert source.isolated is False
