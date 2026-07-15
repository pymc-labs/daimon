"""Fixture-directory tests for daimon.core.skills.discover.

All tests use tmp_path — no network, no real SKILL.md files from the repo tree.
discover_skills is sync, so no async def needed.
"""

from __future__ import annotations

from pathlib import Path

from daimon.core.skills.discover import DiscoveredSkill, discover_skills


def _write_skill(root: Path, name: str, body: str = "body\n") -> Path:
    """Create root/name/SKILL.md with valid frontmatter and return the skill dir."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}")
    return skill_dir


def test_discover_finds_skills_in_nested_dirs(tmp_path: Path) -> None:
    """discover_skills returns one DiscoveredSkill per SKILL.md found."""
    _write_skill(tmp_path, "brainstorming")
    _write_skill(tmp_path, "summarise")

    result = discover_skills(tmp_path)

    assert len(result) == 2, "both skills must be found"
    names = {s.spec.name for s in result}
    assert names == {"brainstorming", "summarise"}, "returned spec names must match frontmatter"


def test_discover_returns_correct_skill_dirs(tmp_path: Path) -> None:
    """DiscoveredSkill.skill_dir points to the directory containing SKILL.md."""
    _write_skill(tmp_path, "brainstorming")

    result = discover_skills(tmp_path)

    assert len(result) == 1
    assert result[0].skill_dir == tmp_path / "brainstorming", (
        "skill_dir must be the directory containing SKILL.md, not the SKILL.md file itself"
    )


def test_discover_skips_both_on_name_collision(tmp_path: Path) -> None:
    """When two SKILL.md files share the same name, both are excluded."""
    _write_skill(tmp_path / "repo-a", "brainstorming")
    _write_skill(tmp_path / "repo-b", "brainstorming")

    result = discover_skills(tmp_path)

    assert result == [], "both skills with colliding name must be excluded"


def test_discover_skips_invalid_frontmatter(tmp_path: Path) -> None:
    """A SKILL.md with no frontmatter delimiters is skipped with a warning, not raised."""
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("No frontmatter here\n")

    result = discover_skills(tmp_path)

    assert result == [], "invalid SKILL.md must be skipped, not raised"


def test_discover_returns_empty_for_no_skills(tmp_path: Path) -> None:
    """Empty directory returns an empty list."""
    result = discover_skills(tmp_path)
    assert result == [], "empty directory must yield empty list"


def test_discover_preserves_skill_dir_path(tmp_path: Path) -> None:
    """DiscoveredSkill.skill_dir is the deep directory, not a shallow parent."""
    deep = tmp_path / "deep" / "nested" / "myskill"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text("---\nname: myskill\ndescription: d\n---\nbody\n")

    result = discover_skills(tmp_path)

    assert len(result) == 1
    assert result[0].skill_dir == tmp_path / "deep" / "nested" / "myskill", (
        "skill_dir must reflect the full nested path"
    )


def test_discover_returns_body_text(tmp_path: Path) -> None:
    """DiscoveredSkill.body contains the markdown body from SKILL.md."""
    _write_skill(tmp_path, "brainstorming", body="This is the body.\n")

    result = discover_skills(tmp_path)

    assert len(result) == 1
    assert "This is the body." in result[0].body, (
        "DiscoveredSkill.body must contain the markdown body from SKILL.md"
    )


def test_discover_result_is_dataclass(tmp_path: Path) -> None:
    """discover_skills returns DiscoveredSkill instances."""
    _write_skill(tmp_path, "brainstorming")

    result = discover_skills(tmp_path)

    assert len(result) == 1
    assert isinstance(result[0], DiscoveredSkill), "each result must be a DiscoveredSkill"
