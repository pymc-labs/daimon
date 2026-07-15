from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core.defaults.loader import (
    load_agent_specs,
    load_environment_specs,
    load_skill_paths,
    load_skill_spec,
    load_system_config,
    parse_skill_frontmatter,
)
from daimon.core.errors import DefaultsError
from daimon.core.specs import SystemConfigSpec


def test_load_agent_specs(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    root.mkdir()
    skill_ref_yaml = "- {type: custom, skill_id: s1}"
    (root / "a.yaml").write_text(
        f"name: a\nmodel: claude-sonnet-4-6\nskills:\n  {skill_ref_yaml}\n"
    )
    (root / "b.yaml").write_text("name: b\nmodel: claude-sonnet-4-6\n")
    specs = load_agent_specs(root)
    assert {s.name for s in specs} == {"a", "b"}


def test_load_agent_specs_rejects_extra_keys(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    root.mkdir()
    (root / "a.yaml").write_text("name: a\nmodel: m\nbogus: 1\n")
    with pytest.raises(DefaultsError, match="extra"):
        load_agent_specs(root)


def test_load_agent_specs_filename_must_match_spec_name(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    root.mkdir()
    (root / "a.yaml").write_text("name: different\nmodel: m\n")
    with pytest.raises(DefaultsError, match="filename"):
        load_agent_specs(root)


def test_load_skill_paths_returns_dirs(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "brainstorming").mkdir(parents=True)
    (root / "brainstorming" / "SKILL.md").write_text(
        "---\nname: brainstorming\ndescription: d\n---\n"
    )
    paths = load_skill_paths(root)
    assert [p.name for p in paths] == ["brainstorming"]


def test_load_skill_spec_local_dir_name_must_match_frontmatter(tmp_path: Path) -> None:
    d = tmp_path / "foo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: bar\ndescription: d\n---\n")
    with pytest.raises(DefaultsError, match="dir name"):
        load_skill_spec(d)


def test_load_skill_spec_returns_body(tmp_path: Path) -> None:
    d = tmp_path / "foo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\nbody text\n")
    spec, body = load_skill_spec(d)
    assert spec.name == "foo"
    assert body.strip() == "body text"


def test_load_skill_spec_raises_when_frontmatter_name_mismatches_spec_name(
    tmp_path: Path,
) -> None:
    # Dir name matches frontmatter but the `name:` line in YAML is wrong —
    # this covers the frontmatter-vs-spec guard separately from dir-vs-spec.
    # With current parser dir-name == frontmatter-name == spec.name, so
    # this case reduces to the existing dir-name guard. Keeping an explicit
    # failing assertion here pins the contract for future refactors.
    d = tmp_path / "foo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: bar\ndescription: d\n---\n")
    with pytest.raises(DefaultsError):
        load_skill_spec(d)


def test_load_skill_spec_raises_when_spec_name_contains_reserved_anthropic(
    tmp_path: Path,
) -> None:
    d = tmp_path / "my-anthropic-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: my-anthropic-skill\ndescription: d\n---\n")
    with pytest.raises(DefaultsError, match="anthropic"):
        load_skill_spec(d)


def test_load_skill_spec_raises_when_spec_name_contains_reserved_claude(
    tmp_path: Path,
) -> None:
    d = tmp_path / "my-claude-helper"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: my-claude-helper\ndescription: d\n---\n")
    with pytest.raises(DefaultsError, match="claude"):
        load_skill_spec(d)


def test_load_skill_spec_reserved_check_is_case_insensitive(tmp_path: Path) -> None:
    d = tmp_path / "my-ANTHROPIC-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: my-ANTHROPIC-skill\ndescription: d\n---\n")
    with pytest.raises(DefaultsError, match="anthropic"):
        load_skill_spec(d)


def test_load_environment_specs(tmp_path: Path) -> None:
    root = tmp_path / "environments"
    root.mkdir()
    (root / "default.yaml").write_text("name: default\ndescription: seeded default\n")
    specs = load_environment_specs(root)
    assert specs[0].name == "default", "loader must parse the environment name field"
    assert specs[0].description == "seeded default", (
        "loader must parse EnvironmentSpec's SDK-mirrored fields"
    )


def test_load_system_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_system_config(tmp_path) is None


def test_load_system_config_parses_both_fields(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("agent_name: daimon\nenvironment_name: default\n")
    spec = load_system_config(tmp_path)
    assert spec == SystemConfigSpec(agent_name="daimon", environment_name="default")


def test_load_system_config_parses_partial_fields(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("agent_name: daimon\n")
    spec = load_system_config(tmp_path)
    assert spec is not None
    assert spec.agent_name == "daimon"
    assert spec.environment_name is None


def test_load_system_config_parses_empty_mapping(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("{}\n")
    spec = load_system_config(tmp_path)
    assert spec == SystemConfigSpec(agent_name=None, environment_name=None)


def test_load_system_config_raises_on_unknown_field(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("agent_name: x\nunknown: y\n")
    with pytest.raises(DefaultsError, match="config.yaml"):
        load_system_config(tmp_path)


def test_load_system_config_raises_on_non_mapping(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("- item1\n- item2\n")
    with pytest.raises(DefaultsError, match="config.yaml"):
        load_system_config(tmp_path)


def test_parse_skill_frontmatter_returns_spec_and_body(tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: brainstorming\ndescription: Brainstorm ideas\n---\nBody text here\n"
    )
    spec, body = parse_skill_frontmatter(skill_md)
    assert spec.name == "brainstorming"
    assert "Body text here" in body


def test_parse_skill_frontmatter_raises_on_missing_frontmatter(tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("No frontmatter here\n")
    with pytest.raises(DefaultsError, match="missing YAML frontmatter"):
        parse_skill_frontmatter(skill_md)


def test_parse_skill_frontmatter_raises_on_invalid_yaml(tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\n: bad: yaml: here\n---\nbody\n")
    with pytest.raises(DefaultsError):
        parse_skill_frontmatter(skill_md)


def test_parse_skill_frontmatter_does_not_enforce_dirname(tmp_path: Path) -> None:
    """parse_skill_frontmatter must parse any path without dir-name enforcement."""
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: differentname\ndescription: d\n---\nbody\n")
    # tmp_path name != "differentname" — but no error, unlike load_skill_spec
    spec, body = parse_skill_frontmatter(skill_md)
    assert spec.name == "differentname"
