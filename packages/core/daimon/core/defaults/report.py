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

    def has_changes(self) -> bool:
        """Whether any outcome is not a no-op skip, across all four resource kinds."""
        return any(
            o.action is not Action.SKIPPED
            for o in (*self.agents, *self.environments, *self.skills, *self.system_config)
        )


def compose_failure_reason(report: ApplyReport) -> str | None:
    """Compose a persisted reason from a report's failed outcomes only, one
    line per failure naming the resource kind, name, and its recorded error.
    Pure -- no I/O. Returns None when the report has no failures.

    Composed ONLY from the outcome's own recorded fields -- never a raw
    exception repr of a credentialed request -- so a provider secret or
    request body can never land in an operator-visible column."""
    failures = [
        o
        for o in (*report.agents, *report.environments, *report.skills, *report.system_config)
        if o.action is Action.FAILED
    ]
    if not failures:
        return None
    return "\n".join(f"{o.kind} {o.name!r}: {o.error}" for o in failures)


VerificationStatus = Literal["in_sync", "diverged", "unverifiable"]


@dataclass(frozen=True)
class ChangedResource:
    """One resource a dry-run reconcile reports would create, update, archive, or delete."""

    kind: ResourceKind
    name: str


@dataclass(frozen=True)
class VerificationOutcome:
    """Classification of a dry-run report comparing live resources to the shipped spec.

    - "in_sync": every outcome was SKIPPED — a real reconcile would change nothing.
    - "diverged": at least one outcome would create/update/archive/delete a resource;
      `changed` names each one.
    - "unverifiable": at least one outcome FAILED, meaning the comparison itself could
      not be made (e.g. a provider read failed) — distinct from divergence, since a
      failure says nothing about whether the resource actually matches the spec.
    """

    status: VerificationStatus
    changed: tuple[ChangedResource, ...] = ()


def classify_verification(report: ApplyReport) -> VerificationOutcome:
    """Classify a dry-run ApplyReport as in sync, diverged, or unverifiable.

    A FAILED outcome takes priority over any divergence found elsewhere in the
    report: "the comparison could not be made" is a different condition from
    "the comparison was made and found a difference", and callers must be able
    to tell them apart.
    """
    outcomes = (*report.agents, *report.environments, *report.skills, *report.system_config)
    if any(o.action is Action.FAILED for o in outcomes):
        return VerificationOutcome(status="unverifiable")
    changed = tuple(
        ChangedResource(kind=o.kind, name=o.name)
        for o in outcomes
        if o.action is not Action.SKIPPED
    )
    if changed:
        return VerificationOutcome(status="diverged", changed=changed)
    return VerificationOutcome(status="in_sync")
