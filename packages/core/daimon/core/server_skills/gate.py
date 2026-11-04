"""What an upstream skill update changes, and whether it waits for approval. Pure."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from daimon.core.mcp_skills.manifest import SkillEntry
from daimon.core.skills.ingest import is_script
from pydantic import JsonValue

__all__ = [
    "AttachedAgent",
    "Changes",
    "Repin",
    "allowed_tools_of",
    "diff_manifests",
    "manifest_digest",
    "manifest_of",
    "may_run",
    "needs_approval",
    "plan_repins",
]


# Files that only hold text or data. Anything else could be run, so a change to it waits.
_INERT_SUFFIXES: Final = frozenset(
    {
        ".csv",
        ".gif",
        ".jpeg",
        ".jpg",
        ".json",
        ".md",
        ".pdf",
        ".png",
        ".svg",
        ".tsv",
        ".txt",
        ".webp",
        ".yaml",
        ".yml",
    }
)


def may_run(path: str, body: bytes) -> bool:
    """Whether a skill file could be run: a script, or anything not known to be inert."""
    return is_script(path, body) or PurePosixPath(path).suffix.lower() not in _INERT_SUFFIXES


def manifest_of(entry: SkillEntry) -> dict[str, str]:
    """Each file's sha256 hex digest by path, as the server lists it."""
    return {f.path: f.digest.removeprefix("sha256:") for f in entry.files}


def manifest_digest(manifest: Mapping[str, str]) -> str:
    """sha256 over the sorted (path, sha256) pairs: names the files, whoever serves them."""
    pairs = sorted(manifest.items())
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def allowed_tools_of(frontmatter: Mapping[str, JsonValue]) -> JsonValue:
    return frontmatter.get("allowed-tools")


@dataclass(frozen=True, slots=True)
class Changes:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def as_json(self) -> dict[str, list[str]]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
        }


def diff_manifests(old: Mapping[str, str], new: Mapping[str, str]) -> Changes:
    return Changes(
        added=tuple(sorted(new.keys() - old.keys())),
        removed=tuple(sorted(old.keys() - new.keys())),
        changed=tuple(sorted(p for p in old.keys() & new.keys() if old[p] != new[p])),
    )


def needs_approval(
    changes: Changes,
    files: Mapping[str, bytes],
    *,
    old_tools: JsonValue,
    new_tools: JsonValue,
) -> bool:
    """An added or changed file that may run, or a changed `allowed-tools`, waits for a person."""
    touched = (*changes.added, *changes.changed)
    return old_tools != new_tools or any(may_run(p, files.get(p, b"")) for p in touched)


@dataclass(frozen=True, slots=True)
class AttachedAgent:
    """An agent with the skill attached, at the version it pins."""

    agent_id: str
    version: str | None


@dataclass(frozen=True, slots=True)
class Repin:
    agent_id: str
    version: str


def plan_repins(
    agents: Iterable[AttachedAgent], *, version: str, pinned: Iterable[str]
) -> list[Repin]:
    """The following agents to move to `version`, a concrete deployed version."""
    if version in ("", "latest"):
        raise ValueError("repins need a concrete version")
    held = set(pinned)
    return [
        Repin(agent.agent_id, version)
        for agent in agents
        if agent.agent_id not in held and agent.version != version
    ]
