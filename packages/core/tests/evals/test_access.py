"""Who may do what on one suite (design: owners and admins all, shares by role, groups live)."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.evals.access import (
    Action,
    Share,
    Subject,
    WorkspaceRole,
    allows,
    held_groups,
    suite_role,
)

TENANT = uuid.uuid4()
SHARES = [
    Share(kind="person", id="u-editor", role="editor"),
    Share(kind="person", id="u-reviewer", role="reviewer"),
    Share(kind="person", id="u-viewer", role="viewer"),
    Share(kind="group", id="g-team", role="reviewer"),
]
ACTIONS: tuple[Action, ...] = ("view", "review", "edit", "export", "share", "delete")


def _subject(
    key: str, role: WorkspaceRole = "member", groups: frozenset[str] | None = None
) -> Subject:
    async def lookup(wanted: frozenset[str]) -> frozenset[str] | None:
        return None if groups is None else wanted & groups

    return Subject(tenant_id=TENANT, platform="web", key=key, role=role, groups=lookup)


@pytest.mark.parametrize(
    ("subject", "allowed"),
    [
        (_subject("u-owner"), set(ACTIONS)),
        (_subject("u-admin", role="admin"), set(ACTIONS)),
        (_subject("u-editor"), {"view", "review", "edit", "export"}),
        (_subject("u-reviewer"), {"view", "review"}),
        (_subject("u-viewer"), {"view"}),
        (_subject("u-member", groups=frozenset({"g-team"})), {"view", "review"}),
        (_subject("u-member"), set()),
        (_subject("u-viewer", role="visitor"), set()),
    ],
    ids=["owner", "admin", "editor", "reviewer", "viewer", "group", "unknown-group", "visitor"],
)
async def test_suite_role_when_subject_varies_then_actions_follow_the_table(
    subject: Subject, allowed: set[Action]
) -> None:
    groups = await held_groups(subject, SHARES)
    role = suite_role(subject, owner_key="u-owner", shares=SHARES, groups=groups)
    got = {action for action in ACTIONS if allows(role, action)}
    assert got == allowed, f"{subject.key} as {subject.role} may {sorted(got)}"
