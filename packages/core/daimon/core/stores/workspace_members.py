"""Store for workspace_members: who belongs to each chat workspace, as its platform says.

The chat adapters write it (a full reload, then events); the web app and sign-in read it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import cast

from daimon.core._models import PlatformPrincipal, Tenant, WorkspaceMember, WorkspaceMemberSync
from daimon.core.member_reloads import MemberState, MemberValue
from daimon.core.stores.domain import CHAT_PLATFORMS, ChatStandingRow
from sqlalchemy import bindparam, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.schema import Table

_BATCH = 1000


def _state(value: MemberValue) -> MemberState:
    return value if isinstance(value, MemberState) else MemberState(is_admin=value)


async def replace_members(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    members: Mapping[str, MemberValue],
    workspace_name: str,
    now: datetime,
) -> None:
    """Make `members` (platform user id to admin flag, or full state) the workspace's whole list.

    Atomic in the caller's transaction, which also marks the list as loaded in full.
    """
    await session.execute(delete(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id))
    rows = [
        {
            "tenant_id": tenant_id,
            "platform_user_id": user_id,
            "is_admin": state.is_admin,
            "role_ids": None if state.role_ids is None else list(state.role_ids),
            "updated_at": now,
        }
        for user_id, state in ((user_id, _state(v)) for user_id, v in members.items())
    ]
    for start in range(0, len(rows), _BATCH):
        await session.execute(insert(WorkspaceMember), rows[start : start + _BATCH])
    new = insert(WorkspaceMemberSync).values(
        tenant_id=tenant_id, workspace_name=workspace_name, synced_at=now
    )
    await session.execute(
        new.on_conflict_do_update(
            index_elements=[WorkspaceMemberSync.tenant_id],
            set_={"workspace_name": workspace_name, "synced_at": now},
        )
    )


async def upsert_member(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    is_admin: bool,
    now: datetime,
    role_ids: Sequence[str] | None = None,
) -> None:
    """Add a member who joined, or record their admin flag and, when given, their role ids."""
    role_list = None if role_ids is None else list(role_ids)
    new = insert(WorkspaceMember).values(
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        is_admin=is_admin,
        role_ids=role_list,
        updated_at=now,
    )
    changed: dict[str, object] = {"is_admin": is_admin, "updated_at": now}
    if role_list is not None:
        changed["role_ids"] = role_list
    await session.execute(
        new.on_conflict_do_update(
            index_elements=[WorkspaceMember.tenant_id, WorkspaceMember.platform_user_id],
            set_=changed,
        )
    )


async def delete_member(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform_user_id: str
) -> None:
    """Remove a member who left, was kicked or banned."""
    await session.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.tenant_id == tenant_id,
            WorkspaceMember.platform_user_id == platform_user_id,
        )
    )


async def apply_changes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    changes: Mapping[str, MemberValue | None],
    now: datetime,
) -> None:
    """Write member changes again: a flag or state lists the user with it, None removes them.

    A change with no role ids leaves a stored list alone."""
    gone = [user_id for user_id, change in changes.items() if change is None]
    for start in range(0, len(gone), _BATCH):
        await session.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.tenant_id == tenant_id,
                WorkspaceMember.platform_user_id.in_(gone[start : start + _BATCH]),
            )
        )
    listed = [
        {
            "tenant_id": tenant_id,
            "platform_user_id": user_id,
            "is_admin": state.is_admin,
            "role_ids": None if state.role_ids is None else list(state.role_ids),
            "updated_at": now,
        }
        for user_id, state in ((u, _state(c)) for u, c in changes.items() if c is not None)
    ]
    for start in range(0, len(listed), _BATCH):
        new = insert(WorkspaceMember).values(listed[start : start + _BATCH])
        await session.execute(
            new.on_conflict_do_update(
                index_elements=[WorkspaceMember.tenant_id, WorkspaceMember.platform_user_id],
                set_={
                    "is_admin": new.excluded.is_admin,
                    "role_ids": func.coalesce(new.excluded.role_ids, WorkspaceMember.role_ids),
                    "updated_at": now,
                },
            )
        )


async def update_admin_flags(
    session: AsyncSession, *, tenant_id: uuid.UUID, flags: Mapping[str, bool], now: datetime
) -> None:
    """Set the admin flag of members already listed; ids not listed are left out.

    In batches, so a whole large workspace stays within statement limits."""
    for is_admin in (True, False):
        user_ids = [user_id for user_id, flag in flags.items() if flag is is_admin]
        for start in range(0, len(user_ids), _BATCH):
            await session.execute(
                update(WorkspaceMember)
                .where(
                    WorkspaceMember.tenant_id == tenant_id,
                    WorkspaceMember.platform_user_id.in_(user_ids[start : start + _BATCH]),
                    WorkspaceMember.is_admin.is_not(is_admin),
                )
                .values(is_admin=is_admin, updated_at=now)
            )


async def update_member_states(
    session: AsyncSession, *, tenant_id: uuid.UUID, states: Mapping[str, MemberState], now: datetime
) -> None:
    """Set the admin flag and role ids of members already listed; ids not listed are left out."""
    if not states:
        return
    table = cast("Table", WorkspaceMember.__table__)
    statement = (
        update(table)
        .where(table.c.tenant_id == tenant_id, table.c.platform_user_id == bindparam("b_user"))
        .values(is_admin=bindparam("b_admin"), role_ids=bindparam("b_roles"), updated_at=now)
    )
    rows = [
        {
            "b_user": user_id,
            "b_admin": state.is_admin,
            "b_roles": None if state.role_ids is None else list(state.role_ids),
        }
        for user_id, state in states.items()
    ]
    for start in range(0, len(rows), _BATCH):
        await session.execute(statement, rows[start : start + _BATCH])


async def member_role_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str
) -> tuple[str, ...] | None:
    """The roles the mirror says `user_id` holds now.

    None when the mirror cannot say: the person is not listed (the list is not loaded, or
    they joined since), or their row has no role ids yet.
    """
    row = (
        await session.execute(
            select(WorkspaceMember.role_ids).where(
                WorkspaceMember.tenant_id == tenant_id,
                WorkspaceMember.platform_user_id == user_id,
            )
        )
    ).one_or_none()
    return None if row is None or row[0] is None else tuple(row[0])


async def member_is_internal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    internal_role_ids: Collection[str],
) -> bool | None:
    """Whether the mirror says `user_id` holds an internal role; None when it cannot say."""
    roles = await member_role_ids(session, tenant_id=tenant_id, user_id=user_id)
    return None if roles is None else bool(set(roles) & set(internal_role_ids))


async def clear_members(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Forget the workspace's list, for an install that ended."""
    await session.execute(delete(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id))
    await session.execute(
        delete(WorkspaceMemberSync).where(WorkspaceMemberSync.tenant_id == tenant_id)
    )


async def clear_platform_lists(session: AsyncSession, *, platform: str) -> None:
    """Forget every loaded list of a platform's workspaces, for a gateway that no longer
    sends member events: a stale list would keep counting as the whole truth, where none
    leaves each sign-in to decide."""
    tenant_ids = select(Tenant.id).where(Tenant.platform == platform)
    await session.execute(delete(WorkspaceMember).where(WorkspaceMember.tenant_id.in_(tenant_ids)))
    await session.execute(
        delete(WorkspaceMemberSync).where(WorkspaceMemberSync.tenant_id.in_(tenant_ids))
    )


async def list_member_tenant_ids(
    session: AsyncSession, *, platform_user_id: str, tenant_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of `tenant_ids` list `platform_user_id` as a member."""
    if not tenant_ids:
        return set()
    rows = await session.execute(
        select(WorkspaceMember.tenant_id).where(
            WorkspaceMember.platform_user_id == platform_user_id,
            WorkspaceMember.tenant_id.in_(tenant_ids),
        )
    )
    return set(rows.scalars())


async def list_synced(
    session: AsyncSession, *, tenant_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The workspaces among `tenant_ids` whose list was loaded in full, with their names."""
    if not tenant_ids:
        return {}
    rows = await session.execute(
        select(WorkspaceMemberSync.tenant_id, WorkspaceMemberSync.workspace_name).where(
            WorkspaceMemberSync.tenant_id.in_(tenant_ids)
        )
    )
    return {tenant_id: name for tenant_id, name in rows.tuples()}


async def list_chat_standings(
    session: AsyncSession, *, people: Collection[tuple[uuid.UUID, uuid.UUID]]
) -> dict[tuple[uuid.UUID, uuid.UUID], ChatStandingRow]:
    """For each `(tenant id, account id)`, the account's chat account in that workspace
    and whether the member list has it. Pairs with no chat account are left out.

    An account with two ids on the workspace's platform stands by the oldest, the one
    its turns run as (`web_identity.linked_identity`).
    """
    if not people:
        return {}
    rows = await session.execute(
        select(
            PlatformPrincipal.tenant_id,
            PlatformPrincipal.account_id,
            PlatformPrincipal.platform,
            PlatformPrincipal.external_id,
            WorkspaceMemberSync.tenant_id.is_not(None),
            WorkspaceMember.platform_user_id.is_not(None),
            WorkspaceMember.is_admin,
        )
        .join(
            Tenant,
            (Tenant.id == PlatformPrincipal.tenant_id)
            & (Tenant.platform == PlatformPrincipal.platform),
        )
        .outerjoin(
            WorkspaceMemberSync, WorkspaceMemberSync.tenant_id == PlatformPrincipal.tenant_id
        )
        .outerjoin(
            WorkspaceMember,
            (WorkspaceMember.tenant_id == PlatformPrincipal.tenant_id)
            & (WorkspaceMember.platform_user_id == PlatformPrincipal.external_id),
        )
        .where(
            tuple_(PlatformPrincipal.tenant_id, PlatformPrincipal.account_id).in_(list(people)),
            PlatformPrincipal.platform.in_(CHAT_PLATFORMS),
        )
        .order_by(PlatformPrincipal.created_at.desc())
    )
    standings: dict[tuple[uuid.UUID, uuid.UUID], ChatStandingRow] = {}
    # Newest first, so the oldest is written last and wins.
    for tenant_id, account_id, platform, user_id, synced, member, is_admin in rows.tuples():
        standings[(tenant_id, account_id)] = ChatStandingRow(
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            platform_user_id=user_id,
            synced=synced,
            member=member,
            is_admin=bool(is_admin),
        )
    return standings
