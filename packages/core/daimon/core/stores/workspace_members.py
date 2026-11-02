"""Store for workspace_members: who belongs to each chat workspace, as its platform says.

The chat adapters write it (a full reload, then events); the web app and sign-in read it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from datetime import datetime

from daimon.core._models import PlatformPrincipal, Tenant, WorkspaceMember, WorkspaceMemberSync
from daimon.core.stores.domain import CHAT_PLATFORMS, ChatStandingRow
from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

_BATCH = 1000


async def replace_members(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    members: Mapping[str, bool],
    workspace_name: str,
    now: datetime,
) -> None:
    """Make `members` (platform user id to admin flag) the workspace's whole list.

    Atomic in the caller's transaction, which also marks the list as loaded in full.
    """
    await session.execute(delete(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id))
    rows = [
        {
            "tenant_id": tenant_id,
            "platform_user_id": user_id,
            "is_admin": is_admin,
            "updated_at": now,
        }
        for user_id, is_admin in members.items()
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
) -> None:
    """Add a member who joined, or record their admin flag."""
    new = insert(WorkspaceMember).values(
        tenant_id=tenant_id, platform_user_id=platform_user_id, is_admin=is_admin, updated_at=now
    )
    await session.execute(
        new.on_conflict_do_update(
            index_elements=[WorkspaceMember.tenant_id, WorkspaceMember.platform_user_id],
            set_={"is_admin": is_admin, "updated_at": now},
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
    changes: Mapping[str, bool | None],
    now: datetime,
) -> None:
    """Write member changes again: an admin flag lists the user with it, None removes them."""
    gone = [user_id for user_id, change in changes.items() if change is None]
    for start in range(0, len(gone), _BATCH):
        await session.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.tenant_id == tenant_id,
                WorkspaceMember.platform_user_id.in_(gone[start : start + _BATCH]),
            )
        )
    listed = [
        {"tenant_id": tenant_id, "platform_user_id": user_id, "is_admin": change, "updated_at": now}
        for user_id, change in changes.items()
        if change is not None
    ]
    for start in range(0, len(listed), _BATCH):
        new = insert(WorkspaceMember).values(listed[start : start + _BATCH])
        await session.execute(
            new.on_conflict_do_update(
                index_elements=[WorkspaceMember.tenant_id, WorkspaceMember.platform_user_id],
                set_={"is_admin": new.excluded.is_admin, "updated_at": now},
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
