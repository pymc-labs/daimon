"""Outcome/report types for `apply_defaults`.

`ResourceOutcome` is one per YAML file / skill dir / swept row. `ApplyReport`
aggregates by resource kind. Both are pure data. The CLI plan builds its
rich-table / JSON output from this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class Action(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"
    ARCHIVED = "archived"
    DELETED = "deleted"
    FAILED = "failed"


ResourceKind = Literal["agent", "environment", "skill", "system_config"]


@dataclass
class ResourceOutcome:
    kind: ResourceKind
    name: str
    action: Action
    anthropic_id: str | None = None
    error: str | None = None


@dataclass
class ApplyReport:
    agents: list[ResourceOutcome] = field(default_factory=list[ResourceOutcome])
    environments: list[ResourceOutcome] = field(default_factory=list[ResourceOutcome])
    skills: list[ResourceOutcome] = field(default_factory=list[ResourceOutcome])
    system_config: list[ResourceOutcome] = field(default_factory=list[ResourceOutcome])

    def add(self, outcome: ResourceOutcome) -> None:
        bucket = {
            "agent": self.agents,
            "environment": self.environments,
            "skill": self.skills,
            "system_config": self.system_config,
        }
        bucket[outcome.kind].append(outcome)

    def is_failure(self) -> bool:
        return any(
            o.action is Action.FAILED
            for o in (*self.agents, *self.environments, *self.skills, *self.system_config)
        )
