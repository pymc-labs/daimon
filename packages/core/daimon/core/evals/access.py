"""Who may do what with evals.

Workspace owners and admins see and manage everything. Members create suites and see what
they own or what is shared with them. On a suite, the owner does everything; an editor
edits cases, starts runs and exports scrubbed bundles; a reviewer sees and reviews; a viewer
sees. Visitors get nothing. Group shares are checked live; unknown membership denies.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Final, Literal

from daimon.core.dm_access import DmGroups, resolve_groups
from daimon.core.evals.format import FormatModel

WorkspaceRole = Literal["owner", "admin", "member", "visitor"]
SuiteRole = Literal["viewer", "reviewer", "editor", "owner"]
ShareRole = Literal["viewer", "reviewer", "editor"]
Action = Literal["view", "review", "edit", "export", "share", "delete"]
GroupLookup = Callable[[frozenset[str]], Awaitable[frozenset[str] | None]]
"""Which of the named groups the subject is in now; None when the platform cannot say."""

_RANK: Final[dict[SuiteRole, int]] = {"viewer": 0, "reviewer": 1, "editor": 2, "owner": 3}
_NEEDS: Final[dict[Action, SuiteRole]] = {
    "view": "viewer",
    "review": "reviewer",
    "edit": "editor",
    "export": "editor",
    "share": "owner",
    "delete": "owner",
}


class Share(FormatModel):
    """A person or group a suite is shared with. Ids are platform ids as sign-ins carry them."""

    kind: Literal["person", "group"]
    id: str
    role: ShareRole


async def _no_groups(wanted: frozenset[str]) -> frozenset[str] | None:
    return None


@dataclass(frozen=True)
class Subject:
    """Whoever is acting: their workspace role, every id they are known by, their groups."""

    tenant_id: uuid.UUID
    platform: str
    key: str
    """The id that owns what they create, such as `web:<person id>` or a platform user id."""
    role: WorkspaceRole
    person_ids: frozenset[str] = frozenset()
    groups: GroupLookup = field(default=_no_groups)
    operator: bool = False
    """Whoever runs Daimon, from the command line: past the feature switch, an admin."""

    @property
    def is_admin(self) -> bool:
        return self.operator or self.role in ("owner", "admin")

    @property
    def ids(self) -> frozenset[str]:
        return self.person_ids | {self.key}


def platform_groups(lookup: DmGroups, user_id: str, *, tenant_id: uuid.UUID) -> GroupLookup:
    """A live group lookup from the platform's own (`dm_access`); failures read as unknown."""

    async def groups(wanted: frozenset[str]) -> frozenset[str] | None:
        return await resolve_groups(lookup, user_id, wanted, tenant_id=tenant_id)

    return groups


async def held_groups(subject: Subject, shares: Iterable[Share]) -> frozenset[str]:
    """The shared groups the subject is in now; none when the platform cannot say."""
    wanted = frozenset(s.id for s in shares if s.kind == "group")
    return (await subject.groups(wanted) or frozenset()) if wanted else frozenset()


def suite_role(
    subject: Subject, *, owner_key: str, shares: Iterable[Share], groups: frozenset[str]
) -> SuiteRole | None:
    """The subject's role on one suite, or None for no access; `groups` from `held_groups`."""
    if subject.role == "visitor" and not subject.operator:
        return None
    if subject.is_admin or owner_key in subject.ids:
        return "owner"
    roles: list[SuiteRole] = []
    roles.extend(
        s.role
        for s in shares
        if (s.kind == "person" and s.id in subject.ids) or (s.kind == "group" and s.id in groups)
    )
    return max(roles, key=_RANK.__getitem__, default=None)


def allows(role: SuiteRole | None, action: Action) -> bool:
    return role is not None and _RANK[role] >= _RANK[_NEEDS[action]]
