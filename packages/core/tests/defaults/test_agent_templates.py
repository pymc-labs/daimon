"""`template: true` agents are starting points for the web app, never provisioned."""

from __future__ import annotations

from pathlib import Path

from daimon.core.defaults.loader import (
    load_agent_specs,
    load_agent_templates,
    load_skill_descriptions,
)


def _agent(root: Path, name: str, *, template: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(
        f"name: {name}\nmodel: claude-sonnet-4-6\ndescription: Does {name}\ntemplate: {template}\n"
    )


def test_a_template_is_listed_as_one_and_left_out_of_what_is_provisioned(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    _agent(root, "seeded", template=False)
    _agent(root, "writer", template=True)
    assert [s.name for s in load_agent_specs(root)] == ["seeded"], "reconcile never sees it"
    assert [s.name for s in load_agent_templates(root)] == ["writer"]


def test_a_template_stays_out_of_the_spec_fingerprint_payload(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    _agent(root, "writer", template=True)
    (spec,) = load_agent_templates(root)
    assert "template" not in spec.model_dump(), "it never reaches an agents.create payload"


def test_skill_descriptions_come_from_the_frontmatter(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "tidy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tidy\ndescription: Keep it clean.\n---\nBody\n")
    assert load_skill_descriptions(tmp_path / "skills") == {"tidy": "Keep it clean."}
    assert load_skill_descriptions(tmp_path / "none") == {}, "no skills directory, no skills"
