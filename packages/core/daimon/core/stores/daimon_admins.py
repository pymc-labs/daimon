"""Tenant-scoped delegation. Callers own transactions and attest platform authority live."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from daimon.core._models import Account, DaimonAdmin, PlatformPrincipal
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.tenants import get_tenant
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, false, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

SubjectKind = Literal["user", "role"]


class AdminGrant(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)
    tenant_id: uuid.UUID
    platform: str
    subject_kind: SubjectKind
    subject_id: str
    granted_by: str | None
    granted_at: datetime


async def list_admins(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[AdminGrant]:
    rows = await session.scalars(
        select(DaimonAdmin)
        .where(DaimonAdmin.tenant_id == tenant_id)
        .order_by(DaimonAdmin.subject_kind, DaimonAdmin.subject_id)
    )
    return [AdminGrant.model_validate(row) for row in rows]


async def has_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_id: str,
    role_ids: Sequence[str] = (),
) -> bool:
    tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.platform != platform or tenant.archived_at is not None:
        return False
    return (
        await session.scalar(
            select(DaimonAdmin.subject_id)
            .where(
                DaimonAdmin.tenant_id == tenant_id,
                DaimonAdmin.platform == platform,
                or_(
                    (DaimonAdmin.subject_kind == "user") & (DaimonAdmin.subject_id == user_id),
                    (DaimonAdmin.subject_kind == "role") & (DaimonAdmin.subject_id.in_(role_ids))
                    if platform == "discord"
                    else false(),
                ),
            )
            .limit(1)
        )
        is not None
    )


async def change_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    subject_kind: SubjectKind,
    subject_id: str,
    actor_user_id: str,
    is_platform_admin: bool,
    revoke: bool = False,
) -> None:
    """Grant/revoke and audit atomically. A Daimon admin bit is never acceptable here.

    Revoke demotes affected accounts in this transaction, including all stored
    holders of a Discord role. Their next live check restores platform authority
    or any remaining grant. There is no API for revoking platform authority.
    """
    if is_platform_admin is not True:
        raise PermissionError("Only platform admins may grant or revoke Daimon admins")
    tenant = await get_tenant(session, tenant_id)
    if (
        tenant is None
        or tenant.platform != platform
        or tenant.archived_at is not None
        or platform not in {"discord", "slack", "teams"}
        or subject_kind not in {"user", "role"}
        or (subject_kind == "role" and platform != "discord")
        or not subject_id
        or subject_id != subject_id.strip()
        or not actor_user_id
    ):
        raise ValueError("Invalid tenant or admin subject")
    key = dict(
        tenant_id=tenant_id, platform=platform, subject_kind=subject_kind, subject_id=subject_id
    )
    # Serialize grants/revokes for the same tenant, including absent grants.
    from daimon.core._models import Tenant

    await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    if revoke:
        await session.execute(delete(DaimonAdmin).filter_by(**key))
        affected = (
            select(PlatformPrincipal.account_id)
            .join(Account, Account.id == PlatformPrincipal.account_id)
            .where(
                PlatformPrincipal.tenant_id == tenant_id,
                PlatformPrincipal.platform == platform,
                PlatformPrincipal.external_id == subject_id
                if subject_kind == "user"
                else true()
                if subject_id == tenant.external_id
                else Account.platform_role_ids.contains([subject_id]),
            )
        )
        await session.execute(
            update(Account).where(Account.id.in_(affected)).values(role=Role.USER.value)
        )
    else:
        await session.execute(
            insert(DaimonAdmin).values(**key, granted_by=actor_user_id).on_conflict_do_nothing()
        )
    actor = await session.scalar(
        select(PlatformPrincipal).where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == actor_user_id,
        )
    )
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=actor.account_id if actor else None,
        agent_id=None,
        platform=platform,
        platform_user_id=actor_user_id,
        tool_name="daimon_admins",
        operation="revoke" if revoke else "grant",
        outcome="allowed",
        reason="platform_admin",
        admin_subject_kind=subject_kind,
        admin_subject_id=subject_id,
    )


async def erase_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> int:
    """Erase both grant subjects and grantors; audit targets are scrubbed separately."""
    count = await count_user_rows(
        session, tenant_id=tenant_id, platform=platform, platform_user_id=platform_user_id
    )
    await session.execute(
        delete(DaimonAdmin).where(
            DaimonAdmin.tenant_id == tenant_id,
            DaimonAdmin.platform == platform,
            DaimonAdmin.subject_kind == "user",
            DaimonAdmin.subject_id == platform_user_id,
        )
    )
    await session.execute(
        update(DaimonAdmin)
        .where(
            DaimonAdmin.tenant_id == tenant_id,
            DaimonAdmin.platform == platform,
            DaimonAdmin.granted_by == platform_user_id,
        )
        .values(granted_by=None)
    )

    return count


async def count_user_rows(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(DaimonAdmin)
            .where(
                DaimonAdmin.tenant_id == tenant_id,
                DaimonAdmin.platform == platform,
                or_(
                    (DaimonAdmin.subject_kind == "user")
                    & (DaimonAdmin.subject_id == platform_user_id),
                    DaimonAdmin.granted_by == platform_user_id,
                ),
            )
        )
        or 0
    )


async def lock_authority(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Hold a shared tenant lock while deriving and persisting aggregate authority.

    Revocation takes the exclusive lock, so an in-flight role write cannot
    resurrect the aggregate role after revoke has demoted its holders.
    """
    from daimon.core._models import Tenant

    await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(read=True))
