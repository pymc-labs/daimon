"""Walk a repo root directory for SKILL.md files and parse them into DiscoveredSkill records.

Name collisions (two SKILL.md files with the same ``spec.name`` within a single
discovery pass) are detected: both colliding skills are skipped and a warning is
logged. Invalid frontmatter is also skipped with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog
from daimon.core.defaults.loader import RESERVED_SKILL_SUBSTRINGS, parse_skill_frontmatter
from daimon.core.errors import DefaultsError
from daimon.core.specs import SkillSpec

_log = structlog.get_logger(__name__)


@dataclass
class DiscoveredSkill:
    """A skill found during repo discovery.

    ``skill_dir`` is the directory containing ``SKILL.md`` (and any sibling
    files that will be zipped when the skill is synced to MA).
    """

    spec: SkillSpec
    skill_dir: Path
    body: str


def discover_skills(root: Path) -> list[DiscoveredSkill]:
    """Walk ``root`` recursively, find SKILL.md files, and return parsed skills.

    Name collisions are detected: when two SKILL.md files share the same
    ``spec.name``, both are excluded from the result and a warning is logged.
    Invalid frontmatter causes the file to be skipped (with a warning), not
    an abort.

    Returns:
        A list of :class:`DiscoveredSkill` in sorted-path order with no
        duplicate names.
    """
    found: list[DiscoveredSkill] = []
    seen_names: dict[str, Path] = {}

    for skill_md in sorted(root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        try:
            spec, body = parse_skill_frontmatter(skill_md)
        except DefaultsError as err:
            _log.warning("discover.invalid_frontmatter", path=str(skill_md), error=str(err))
            continue

        lowered = spec.name.lower()
        reserved_hit = next((r for r in RESERVED_SKILL_SUBSTRINGS if r in lowered), None)
        if reserved_hit is not None:
            _log.warning(
                "discover.reserved_name",
                path=str(skill_md),
                name=spec.name,
                reserved=reserved_hit,
            )
            continue

        if spec.name in seen_names:
            _log.warning(
                "discover.name_collision",
                name=spec.name,
                first=str(seen_names[spec.name]),
                second=str(skill_dir),
            )
            # Remove the first occurrence too — skip both colliding skills.
            found = [s for s in found if s.spec.name != spec.name]
            continue

        seen_names[spec.name] = skill_dir
        found.append(DiscoveredSkill(spec=spec, skill_dir=skill_dir, body=body))

    return found
