"""Provider-independent ownership decisions for skills copied with an agent.

The host supplies titles and upload ownership from its authorized catalog.
No provider client, native model, lookup or write belongs in this decision.
Positions preserve input order and duplicate pins without reconstructing them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from daimon.core.defaults.metadata import skill_owner_candidates


@dataclass(frozen=True)
class AttachedSkillOwnership:
    id: str
    custom: bool
    title: str | None = None
    version: str | None = None
    latest_version: str | None = None
    upload_owner: str | None = None


@dataclass(frozen=True)
class OwnSkillCopy:
    position: int
    id: str
    title: str
    version: str | None


@dataclass(frozen=True)
class SkillCopyPartition:
    kept_positions: tuple[int, ...]
    own: tuple[OwnSkillCopy, ...]
    dropped: tuple[str, ...]


def partition_copy_skills(
    skills: Sequence[AttachedSkillOwnership],
    *,
    source_name: str,
    agent_names: Sequence[str],
) -> SkillCopyPartition:
    """Keep shared/catalog pins; copy source-owned pins; omit foreign ownership.

    A missing concrete version is retained as a failed-copy input. Resolving
    or downloading it is the operation edge's responsibility, as before.
    """
    if not any(skill.custom for skill in skills):
        return SkillCopyPartition(tuple(range(len(skills))), (), ())
    kept: list[int] = []
    own: list[OwnSkillCopy] = []
    dropped: list[str] = []
    for position, skill in enumerate(skills):
        owners = skill_owner_candidates(
            skill.title or "", stored_owner=skill.upload_owner, agent_names=agent_names
        )
        if not owners:
            kept.append(position)
        elif owners == frozenset({source_name}) and skill.title is not None:
            pin = skill.version
            version = pin if pin and pin != "latest" else skill.latest_version
            own.append(OwnSkillCopy(position, skill.id, skill.title, version))
        else:
            dropped.append(skill.title or skill.id)
    return SkillCopyPartition(tuple(kept), tuple(own), tuple(dropped))
