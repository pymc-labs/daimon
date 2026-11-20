"""Who may do what with a workflow: roles from the owner, the access list and the workspace.

A workflow is private (only its owner) or shared (everyone in the workspace sees it; the
access list names who edits and who runs). A workspace admin holds every right. The rules
are the design doc's roles table; the web routes and the chat tools both ask here.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from daimon.core.stores import workflows as store
from daimon.core.stores.workflows import AccessRole, WorkflowAccessRow, WorkflowRow
from daimon.core.web_roles_rules import is_admin, role_of_account
from sqlalchemy.ext.asyncio import AsyncSession

Role = Literal["owner", "editor", "runner", "viewer"]
_RANK: dict[Role, int] = {"viewer": 0, "runner": 1, "editor": 2, "owner": 3}


@dataclass(frozen=True)
class Who:
    """The caller as the workflow rules see them.

    `external` is an agent key or operator token: it reads, drafts and test-runs for the
    person it belongs to, but never publishes, approves or decides.
    """

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    admin: bool = False
    groups: frozenset[str] = frozenset()
    external: bool = False


@dataclass(frozen=True)
class Rights:
    role: Role | None
    admin: bool

    @property
    def view(self) -> bool:
        return self.admin or self.role is not None

    @property
    def edit(self) -> bool:
        """Edit the draft, test run and publish."""
        return self.admin or self.role in ("owner", "editor")

    @property
    def run(self) -> bool:
        return self.admin or self.role in ("owner", "editor", "runner")

    @property
    def manage(self) -> bool:
        """Access, triggers, secrets, pause and delete."""
        return self.admin or self.role == "owner"

    def sees_run(self, *, own: bool) -> bool:
        return (
            self.admin
            or self.role in ("owner", "editor", "viewer")
            or (self.role == "runner" and own)
        )

    def controls_run(self, *, own: bool) -> bool:
        """Cancel, retry from failure and run from here."""
        return self.admin or self.role in ("owner", "editor") or (self.role == "runner" and own)


def rights_of(workflow: WorkflowRow, access: Collection[WorkflowAccessRow], who: Who) -> Rights:
    """The caller's rights on `workflow`, given its access rows."""
    if workflow.owner_account_id == who.account_id:
        return Rights("owner", who.admin)
    role: Role | None = "viewer" if workflow.visibility == "shared" else None
    if workflow.visibility == "shared":
        for row in access:
            held = row.account_id == who.account_id or (
                row.group_id is not None and row.group_id in who.groups
            )
            if held and _RANK[row.role] > _RANK[role or "viewer"]:
                role = row.role
    return Rights(role, who.admin)


def holders(access: Collection[WorkflowAccessRow], role: AccessRole) -> list[uuid.UUID]:
    """The accounts holding `role` by name; groups are listed apart."""
    return [r.account_id for r in access if r.role == role and r.account_id is not None]


async def who_of(session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID) -> Who | None:
    """A run's person as the rules see them when nobody is calling: None when suspended."""
    role = await role_of_account(
        session, tenant_id=tenant_id, account_id=account_id, chat_admin=False
    )
    return None if role is None else Who(tenant_id, account_id, admin=is_admin(role))


async def may_run(session: AsyncSession, workflow: WorkflowRow, account_id: uuid.UUID) -> bool:
    who = await who_of(session, workflow.tenant_id, account_id)
    if who is None:
        return False
    access = await store.list_access(session, tenant_id=workflow.tenant_id, workflow_id=workflow.id)
    return rights_of(workflow, access, who).run
