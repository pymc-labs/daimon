"""I/O boundary from registered tenant membership to pure authorization facts."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from daimon.core.admin_actions import DelegatedAction, admin_for_action
from daimon.core.authz import Subject, build_subject
from daimon.core.membership import MembershipResolver
from daimon.core.native_admins import is_daimon_admin
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def resolve_subject_authority(
    sessionmaker: async_sessionmaker[AsyncSession],
    resolver: MembershipResolver | None,
    *,
    tenant_id: UUID,
    user_id: str,
    require_fresh: bool = False,
    session: AsyncSession | None = None,
) -> tuple[Subject, bool]:
    """Resolve only the selected, registered, active Discord tenant; fail closed.

    No account UUID or persisted admin flag is used as platform identity.
    Unsupported platforms and absent credentials produce unknown evidence.
    Pass the caller's session when already holding a connection: membership
    checks must not acquire a second pool slot inside an admission/fire.
    """
    unknown = build_subject(is_admin=False, platform_user_id=user_id)
    if session is None:
        async with sessionmaker() as lookup:
            tenant = await get_tenant(lookup, tenant_id)
    else:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.archived_at is not None:
        return build_subject(
            is_admin=False, platform_user_id=user_id, verified_membership=False
        ), False
    if tenant.platform != "discord" or resolver is None:
        return unknown, False
    facts = await resolver.resolve(
        tenant_id, tenant.external_id, user_id, require_fresh=require_fresh
    )

    async def authority(lookup: AsyncSession) -> bool:
        return facts.is_member is True and await is_daimon_admin(
            lookup,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id=user_id,
            is_platform_admin=facts.is_admin,
            role_ids=(*facts.role_ids, tenant.external_id),
        )

    if session is None:
        async with sessionmaker() as lookup:
            admin = await authority(lookup)
    else:
        admin = await authority(session)
    return build_subject(
        is_admin=admin,
        platform_user_id=user_id,
        role_ids=facts.role_ids if facts.is_member is True and not facts.is_bot else (),
        verified_membership=facts.is_member,
    ), facts.is_member is True and facts.is_admin


async def resolve_subject(
    sessionmaker: async_sessionmaker[AsyncSession],
    resolver: MembershipResolver | None,
    *,
    tenant_id: UUID,
    user_id: str,
    require_fresh: bool = False,
    session: AsyncSession | None = None,
    action: DelegatedAction | None = None,
) -> Subject:
    """Resolve current authority; delegated rights require an explicit allowed action."""
    subject, platform_admin = await resolve_subject_authority(
        sessionmaker,
        resolver,
        tenant_id=tenant_id,
        user_id=user_id,
        require_fresh=require_fresh,
        session=session,
    )
    return replace(
        subject,
        is_admin=admin_for_action(
            platform_admin=platform_admin, daimon_admin=subject.is_admin, action=action
        ),
    )
