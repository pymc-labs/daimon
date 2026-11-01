"""Store for the web app's role sources: workspace admins and OIDC group roles."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from typing import Any, cast

from daimon.core._models import WebGroupRole, WebPerson, WorkspaceAdmin
from daimon.core.stores.domain import WebGroupRoleName, WebRoleGrant
from sqlalchemy import CursorResult, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_workspace_role(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> WebRoleGrant | None:
    role = (
        await session.execute(
            select(WorkspaceAdmin.role).where(
                WorkspaceAdmin.tenant_id == tenant_id, WorkspaceAdmin.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    return None if role is None else _grant(role)


def _grant(role: str) -> WebRoleGrant:
    if role not in ("owner", "admin"):
        raise ValueError(f"unknown workspace role {role!r}")
    return "owner" if role == "owner" else "admin"


async def set_workspace_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    role: WebRoleGrant,
    granted_by_account_id: uuid.UUID | None,
) -> None:
    await session.execute(
        insert(WorkspaceAdmin)
        .values(
            tenant_id=tenant_id,
            account_id=account_id,
            role=role,
            granted_by_account_id=granted_by_account_id,
        )
        .on_conflict_do_update(
            index_elements=[WorkspaceAdmin.tenant_id, WorkspaceAdmin.account_id],
            set_={"role": role, "granted_by_account_id": granted_by_account_id},
        )
    )


async def set_group_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    issuer: str,
    group: str,
    role: WebGroupRoleName,
) -> None:
    await session.execute(
        insert(WebGroupRole)
        .values(tenant_id=tenant_id, issuer=issuer, group=group, role=role)
        .on_conflict_do_update(
            index_elements=[WebGroupRole.tenant_id, WebGroupRole.issuer, WebGroupRole.group],
            set_={"role": role},
        )
    )


async def list_group_roles(
    session: AsyncSession, *, tenant_id: uuid.UUID, issuer: str, groups: Sequence[str]
) -> frozenset[str]:
    """The roles the person's groups confer here (`admin`, `member`)."""
    if not groups:
        return frozenset()
    rows = await session.execute(
        select(WebGroupRole.role).where(
            WebGroupRole.tenant_id == tenant_id,
            WebGroupRole.issuer == issuer,
            WebGroupRole.group.in_(list(groups)),
        )
    )
    return frozenset(rows.scalars())


async def list_group_roles_for(
    session: AsyncSession, *, tenant_id: uuid.UUID, groups: Collection[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """The role each `(issuer, group)` pair confers here (`admin`, `member`), in one read;
    a group that confers none is absent."""
    if not groups:
        return {}
    rows = await session.execute(
        select(WebGroupRole.issuer, WebGroupRole.group, WebGroupRole.role).where(
            WebGroupRole.tenant_id == tenant_id,
            tuple_(WebGroupRole.issuer, WebGroupRole.group).in_(list(groups)),
        )
    )
    return {(issuer, group): role for issuer, group, role in rows.all()}


async def has_active_owner(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether an active person holds the owner role; a suspended owner can't act."""
    found = await session.scalar(
        select(WorkspaceAdmin.account_id)
        .join(
            WebPerson,
            (WebPerson.tenant_id == WorkspaceAdmin.tenant_id)
            & (WebPerson.account_id == WorkspaceAdmin.account_id),
        )
        .where(
            WorkspaceAdmin.tenant_id == tenant_id,
            WorkspaceAdmin.role == "owner",
            WebPerson.status == "active",
        )
        .limit(1)
    )
    return found is not None


async def count_admin_grants_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(WorkspaceAdmin)
        .where(WorkspaceAdmin.account_id == account_id)
    )
    return count or 0


async def delete_admin_grants_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    result = await session.execute(
        delete(WorkspaceAdmin).where(WorkspaceAdmin.account_id == account_id)
    )
    return cast(CursorResult[Any], result).rowcount


async def delete_workspace_role(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    result = await session.execute(
        delete(WorkspaceAdmin).where(
            WorkspaceAdmin.tenant_id == tenant_id, WorkspaceAdmin.account_id == account_id
        )
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def list_workspace_roles(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> dict[uuid.UUID, WebRoleGrant]:
    """Every web role granted in a workspace, by account."""
    rows = await session.execute(
        select(WorkspaceAdmin.account_id, WorkspaceAdmin.role).where(
            WorkspaceAdmin.tenant_id == tenant_id
        )
    )
    return {account_id: _grant(role) for account_id, role in rows}
