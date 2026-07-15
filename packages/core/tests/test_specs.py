from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core.specs import (
    AgentSpec,
    EnvironmentSpec,
    SkillRef,
    SkillRepo,
    SkillSpec,
    SystemConfigSpec,
    dump_agent_spec,
    merge_default_agent_toolset,
)
from pydantic import ValidationError


def test_agent_spec_parses_minimal_yaml_dict_when_required_fields_present() -> None:
    spec = AgentSpec.model_validate({"name": "daimon", "model": "claude-sonnet-4-6"})
    assert spec.name == "daimon"
    assert spec.model == "claude-sonnet-4-6"
    assert spec.system is None, "system defaults to None, not empty string"
    assert spec.tools is None, "tools mirrors SDK NotRequired[list] shape"
    assert spec.mcp_servers is None, "mcp_servers mirrors SDK NotRequired[list] shape"
    assert spec.skills == [], "skills defaults to empty list (daimon-local field)"


def test_agent_spec_preserves_tool_and_mcp_and_skill_fields_when_populated() -> None:
    tools = [
        {"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]},
        {"type": "mcp_toolset", "mcp_server_name": "ex"},
    ]
    spec = AgentSpec.model_validate(
        {
            "name": "daimon",
            "model": "claude-sonnet-4-6",
            "system": "You are daimon.",
            "tools": tools,
            "mcp_servers": [{"type": "url", "name": "ex", "url": "https://example.invalid/mcp"}],
            "skills": [
                {"type": "custom", "skill_id": "brainstorming"},
                {"type": "custom", "skill_id": "obra/superpowers/debugging"},
            ],
        }
    )
    assert spec.system == "You are daimon."
    assert spec.tools == tools
    assert spec.mcp_servers == [{"type": "url", "name": "ex", "url": "https://example.invalid/mcp"}]
    assert spec.skills == [
        SkillRef(type="custom", skill_id="brainstorming"),
        SkillRef(type="custom", skill_id="obra/superpowers/debugging"),
    ]


def test_agent_spec_rejects_mcp_servers_without_matching_mcp_toolset() -> None:
    """L2 finding: MA returns 400 when mcp_servers is declared without a
    corresponding mcp_toolset tool entry. Catch this at parse time with a clear
    message instead of letting the SDK surface a vague upstream error.
    """
    with pytest.raises(ValidationError) as excinfo:
        AgentSpec.model_validate(
            {
                "name": "daimon",
                "model": "claude-sonnet-4-6",
                "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
                "mcp_servers": [
                    {"type": "url", "name": "ex", "url": "https://example.invalid/mcp"}
                ],
            }
        )
    msg = str(excinfo.value)
    assert "mcp_toolset" in msg, (
        f"error must name the missing tool type so authors know what to add; got {msg!r}"
    )


def test_agent_spec_rejects_unknown_top_level_key_when_validated() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AgentSpec.model_validate(
            {"name": "daimon", "model": "claude-sonnet-4-6", "temperature": 0.7}
        )
    assert "temperature" in str(excinfo.value), (
        "error should name the rejected key so authors can fix typos"
    )


def test_agent_spec_requires_name_and_model_when_missing() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({"model": "claude-sonnet-4-6"})
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({"name": "daimon"})


def test_agent_spec_roundtrips_through_model_dump_when_dumped_exclude_none() -> None:
    spec = AgentSpec.model_validate(
        {
            "name": "daimon",
            "model": "claude-sonnet-4-6",
            "system": "hi",
            "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
            "mcp_servers": [],
            "skills": [{"type": "custom", "skill_id": "brainstorming"}],
        }
    )
    dumped = spec.model_dump(exclude_none=True)
    assert dumped == {
        "name": "daimon",
        "model": "claude-sonnet-4-6",
        "system": "hi",
        "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
        "mcp_servers": [],
    }, "skills is excluded from model_dump (resolved at SDK boundary), other fields round-trip"


def test_agent_spec_skills_allow_slashes_in_names_when_phase_2_provider_prefixed() -> None:
    """Skill names must allow `/` for provider prefixes."""
    spec = AgentSpec.model_validate(
        {
            "name": "daimon",
            "model": "claude-sonnet-4-6",
            "skills": [{"type": "custom", "skill_id": "obra/superpowers/brainstorming"}],
        }
    )
    assert spec.skills == [SkillRef(type="custom", skill_id="obra/superpowers/brainstorming")]


def test_merge_default_agent_toolset_appends_base_toolset_when_tools_none() -> None:
    merged = merge_default_agent_toolset(None)
    assert len(merged) == 1, "None tools must yield exactly the base toolset"
    toolset = merged[0]
    assert toolset.get("type") == "agent_toolset_20260401"
    config_names = [c.get("name") for c in toolset.get("configs", [])]
    assert config_names == ["bash", "read", "edit", "grep", "glob", "write"], (
        "base toolset must enable all six dev tools — skills require read to be usable"
    )


def test_merge_default_agent_toolset_appends_when_only_mcp_toolset_present() -> None:
    existing = [{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}]
    merged = merge_default_agent_toolset(existing)  # pyright: ignore[reportArgumentType]
    types = [t.get("type") for t in merged]
    assert types == ["mcp_toolset", "agent_toolset_20260401"], (
        "an mcp_toolset alone must not satisfy the base-toolset requirement"
    )


def test_merge_default_agent_toolset_returns_same_object_when_already_present() -> None:
    existing = [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}]
    merged = merge_default_agent_toolset(existing)  # pyright: ignore[reportArgumentType]
    assert merged is existing, (
        "idempotent: an authored agent_toolset must be preserved untouched, no churn"
    )


def test_dump_agent_spec_injects_base_toolset_when_spec_has_no_tools() -> None:
    spec = AgentSpec.model_validate({"name": "daimon", "model": "claude-sonnet-4-6"})
    dumped = dump_agent_spec(spec)
    toolsets = [t for t in dumped["tools"] if t["type"] == "agent_toolset_20260401"]
    assert len(toolsets) == 1, "spec without tools must still produce the base agent_toolset"
    assert toolsets[0]["default_config"]["permission_policy"] == {"type": "always_allow"}, (
        "injected toolset must get the always_allow policy like every other toolset"
    )


def test_dump_agent_spec_injects_base_toolset_when_spec_has_only_mcp_toolset() -> None:
    """The restaurant-agent failure: mcp_servers forces an mcp_toolset into
    tools, which used to skip the base-toolset default — the created agent then
    400s at session create once skills are attached (skills require read)."""
    spec = AgentSpec.model_validate(
        {
            "name": "restaurant-agent",
            "model": "claude-sonnet-4-6",
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
            "mcp_servers": [
                {"type": "url", "name": "daimon-mcp", "url": "https://example.invalid/mcp"}
            ],
        }
    )
    dumped = dump_agent_spec(spec)
    types = [t["type"] for t in dumped["tools"]]
    assert "agent_toolset_20260401" in types, (
        "non-empty tools without an agent_toolset must still gain the base toolset"
    )


def test_dump_agent_spec_preserves_authored_toolset_when_present() -> None:
    spec = AgentSpec.model_validate(
        {
            "name": "daimon",
            "model": "claude-sonnet-4-6",
            "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
        }
    )
    dumped = dump_agent_spec(spec)
    toolsets = [t for t in dumped["tools"] if t["type"] == "agent_toolset_20260401"]
    assert len(toolsets) == 1, "authored toolset must not be duplicated"
    config_names = [c["name"] for c in toolsets[0]["configs"]]
    assert config_names == ["bash"], "authored configs must be preserved verbatim, not replaced"


def test_environment_spec_parses_minimal_yaml_dict_when_only_name_given() -> None:
    spec = EnvironmentSpec.model_validate({"name": "default"})
    assert spec.name == "default"
    assert spec.config is None, "config is NotRequired in the SDK"
    assert spec.description is None, "description is NotRequired in the SDK"


def test_environment_spec_accepts_nested_config_when_provided() -> None:
    spec = EnvironmentSpec.model_validate(
        {
            "name": "default",
            "config": {
                "type": "cloud",
                "packages": {"apt": ["ripgrep"]},
            },
            "description": "the default cloud env",
        }
    )
    assert spec.config == {
        "type": "cloud",
        "packages": {
            "type": "packages",
            "apt": ["ripgrep"],
            "cargo": [],
            "gem": [],
            "go": [],
            "npm": [],
            "pip": [],
        },
    }, "provided apt list kept; other ecosystems filled empty so the spec is authoritative"
    assert spec.description == "the default cloud env"


def test_environment_spec_normalizes_packages_to_empty_arrays_when_config_has_no_packages() -> None:
    spec = EnvironmentSpec.model_validate({"name": "default", "config": {"type": "cloud"}})
    dumped = spec.model_dump(exclude_none=True)
    assert dumped["config"]["packages"] == {
        "type": "packages",
        "apt": [],
        "cargo": [],
        "gem": [],
        "go": [],
        "npm": [],
        "pip": [],
    }, (
        "absent packages key normalizes to explicit empty arrays so update replaces (clears) MA state"
    )


def test_environment_spec_leaves_config_unset_when_absent() -> None:
    spec = EnvironmentSpec.model_validate({"name": "default"})
    assert spec.config is None, "no config key at all means no packages forced onto the env"
    assert "config" not in spec.model_dump(exclude_none=True), "absent config stays absent in dump"


def test_environment_spec_rejects_unknown_top_level_key_when_validated() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EnvironmentSpec.model_validate({"name": "default", "region": "us-east-1"})
    assert "region" in str(excinfo.value)


def test_environment_spec_requires_name_when_missing() -> None:
    with pytest.raises(ValidationError):
        EnvironmentSpec.model_validate({"config": {"type": "cloud"}})


def test_skill_spec_parses_required_name_and_description_when_minimal() -> None:
    spec = SkillSpec.model_validate(
        {"name": "brainstorming", "description": "Turn ideas into designs."}
    )
    assert spec.name == "brainstorming"
    assert spec.description == "Turn ideas into designs."


def test_skill_spec_preserves_extra_frontmatter_keys_when_provided() -> None:
    """Additional frontmatter keys are preserved, not interpreted."""
    raw = {
        "name": "brainstorming",
        "description": "Turn ideas into designs.",
        "author": "obra",
        "tags": ["design", "process"],
    }
    spec = SkillSpec.model_validate(raw)
    dumped = spec.model_dump()
    assert dumped["author"] == "obra"
    assert dumped["tags"] == ["design", "process"]


def test_skill_spec_requires_name_and_description_when_missing() -> None:
    with pytest.raises(ValidationError):
        SkillSpec.model_validate({"description": "no name"})
    with pytest.raises(ValidationError):
        SkillSpec.model_validate({"name": "no-description"})


def test_skill_spec_name_allows_slashes_when_provider_prefixed() -> None:
    """`/` must be allowed in skill names for provider prefixes."""
    spec = SkillSpec.model_validate(
        {"name": "obra/superpowers/brainstorming", "description": "..."}
    )
    assert spec.name == "obra/superpowers/brainstorming"


def test_system_config_spec_accepts_both_fields() -> None:
    spec = SystemConfigSpec.model_validate({"agent_name": "daimon", "environment_name": "default"})
    assert spec.agent_name == "daimon"
    assert spec.environment_name == "default"


def test_system_config_spec_allows_missing_fields() -> None:
    spec = SystemConfigSpec.model_validate({})
    assert spec.agent_name is None
    assert spec.environment_name is None


def test_system_config_spec_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SystemConfigSpec.model_validate({"agent_name": "x", "unknown": "y"})


def test_load_agent_spec_raises_spec_error_when_file_missing(tmp_path: Path) -> None:
    from daimon.core.errors import SpecError
    from daimon.core.specs import load_agent_spec

    missing = tmp_path / "nope.yaml"
    with pytest.raises(SpecError) as excinfo:
        load_agent_spec(missing)
    assert str(missing) in str(excinfo.value), (
        "error message must name the missing path so authors can fix typos"
    )
    assert isinstance(excinfo.value.__cause__, FileNotFoundError), (
        "SpecError must preserve the underlying OSError via __cause__"
    )


def test_load_agent_spec_raises_spec_error_when_yaml_malformed(tmp_path: Path) -> None:
    import yaml as _yaml
    from daimon.core.errors import SpecError
    from daimon.core.specs import load_agent_spec

    bad = tmp_path / "bad.yaml"
    bad.write_text("key: : :\n")
    with pytest.raises(SpecError) as excinfo:
        load_agent_spec(bad)
    assert str(bad) in str(excinfo.value), "error must name the offending file"
    assert isinstance(excinfo.value.__cause__, _yaml.YAMLError), (
        "SpecError must preserve the yaml.YAMLError via __cause__"
    )


def test_load_agent_spec_raises_spec_error_when_schema_violated(tmp_path: Path) -> None:
    from daimon.core.errors import SpecError
    from daimon.core.specs import load_agent_spec

    bad = tmp_path / "bad_schema.yaml"
    bad.write_text("name: x\nmodel: claude-haiku-4-5\nbad_field: xxx\n")
    with pytest.raises(SpecError) as excinfo:
        load_agent_spec(bad)
    assert "bad_field" in str(excinfo.value), (
        "error must name the rejected key so authors can fix typos"
    )
    assert isinstance(excinfo.value.__cause__, ValidationError), (
        "SpecError must preserve the pydantic ValidationError via __cause__"
    )


# --- SkillRef tests ---


def test_skillref_custom_valid() -> None:
    ref = SkillRef(type="custom", skill_id="my-skill")
    assert ref.type == "custom"
    assert ref.skill_id == "my-skill"


def test_skillref_anthropic_valid() -> None:
    ref = SkillRef(type="anthropic", skill_id="xlsx")
    assert ref.type == "anthropic"
    assert ref.skill_id == "xlsx"


def test_skillref_rejects_invalid_type() -> None:
    with pytest.raises(ValidationError):
        SkillRef(type="invalid", skill_id="x")  # type: ignore[arg-type]


def test_skillref_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SkillRef.model_validate({"type": "custom", "skill_id": "x", "extra": "y"})


def test_skillref_frozen() -> None:
    ref = SkillRef(type="custom", skill_id="my-skill")
    with pytest.raises(ValidationError):
        ref.skill_id = "new"  # type: ignore[misc]


def test_agentspec_skills_accepts_skillref() -> None:
    spec = AgentSpec(
        name="a",
        model="claude-sonnet-4-5",
        skills=[SkillRef(type="custom", skill_id="s")],
    )
    assert spec.skills == [SkillRef(type="custom", skill_id="s")]


def test_agentspec_skills_rejects_bare_string() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({"name": "a", "model": "claude-sonnet-4-5", "skills": ["bare"]})


def test_agentspec_skills_excluded_from_dump() -> None:
    spec = AgentSpec(
        name="a",
        model="claude-sonnet-4-5",
        skills=[SkillRef(type="custom", skill_id="s")],
    )
    assert "skills" not in spec.model_dump()


# --- dump_agent_spec always_allow injection tests ---


def test_dump_agent_spec_injects_always_allow_on_agent_toolset() -> None:
    from daimon.core.specs import dump_agent_spec

    spec = AgentSpec.model_validate(
        {
            "name": "a",
            "model": "claude-sonnet-4-6",
            "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
        }
    )
    dumped = dump_agent_spec(spec)
    assert dumped["tools"][0]["default_config"]["permission_policy"] == {"type": "always_allow"}


def test_dump_agent_spec_injects_always_allow_on_mcp_toolset() -> None:
    from daimon.core.specs import dump_agent_spec

    spec = AgentSpec.model_validate(
        {
            "name": "a",
            "model": "claude-sonnet-4-6",
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "example"}],
        }
    )
    dumped = dump_agent_spec(spec)
    assert dumped["tools"][0]["default_config"]["permission_policy"] == {"type": "always_allow"}


def test_dump_agent_spec_preserves_existing_default_config() -> None:
    from daimon.core.specs import dump_agent_spec

    spec = AgentSpec.model_validate(
        {
            "name": "a",
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "type": "agent_toolset_20260401",
                    "configs": [{"name": "bash"}],
                    "default_config": {"enabled": True},
                }
            ],
        }
    )
    dumped = dump_agent_spec(spec)
    tool_cfg = dumped["tools"][0]["default_config"]
    assert tool_cfg["enabled"] is True
    assert tool_cfg["permission_policy"] == {"type": "always_allow"}


def test_dump_agent_spec_no_tools_yields_base_toolset_only() -> None:
    spec = AgentSpec.model_validate({"name": "a", "model": "claude-sonnet-4-6"})
    dumped = dump_agent_spec(spec)
    assert [t["type"] for t in dumped["tools"]] == ["agent_toolset_20260401"], (
        "a toolless spec dumps to exactly the base agent_toolset — never an agent without tools"
    )


def test_skill_repo_constructs_with_defaults() -> None:
    repo = SkillRepo(url="https://github.com/owner/repo")
    assert repo.url == "https://github.com/owner/repo", "url is preserved verbatim"
    assert repo.branch == "main", "branch defaults to 'main'"
    assert repo.path == "", "path defaults to empty string"
    assert repo.split is True, (
        "split defaults to True (per-SKILL.md discovery) so chat-created agents that "
        "rely on the default get every skill in a multi-skill repo, not a hollow bundle"
    )


def test_skill_repo_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        SkillRepo(url="https://github.com/owner/repo", bogus=1)  # type: ignore[call-arg]
    assert "bogus" in str(excinfo.value), "extra='forbid' must reject unknown fields"


def test_skill_repo_is_frozen() -> None:
    repo = SkillRepo(url="https://github.com/owner/repo")
    with pytest.raises(ValidationError):
        repo.url = "https://github.com/other/repo"  # type: ignore[misc]


def test_agent_spec_skill_repos_defaults_to_empty_list() -> None:
    spec = AgentSpec.model_validate({"name": "daimon", "model": "claude-sonnet-4-6"})
    assert spec.skill_repos == [], "skill_repos defaults to empty list (daimon-local field)"


def test_agent_spec_skill_repos_excluded_from_dump() -> None:
    spec = AgentSpec(
        name="daimon",
        model="claude-sonnet-4-6",
        skill_repos=[SkillRepo(url="https://github.com/owner/repo")],
    )
    dumped = spec.model_dump()
    assert "skill_repos" not in dumped, (
        "skill_repos must be excluded from model_dump (does not serialize to MA)"
    )
    assert spec.skill_repos == [SkillRepo(url="https://github.com/owner/repo")], (
        "skill_repos still readable on the model instance after construction"
    )
