"""Platform-edge stand-in for tests whose actors are declared by seeded accounts.

This supplies the platform response in transport/discovery tests. Authorization
regressions use real minting and platform HTTP fakes instead. Product code must
never import this fixture: stored account roles do not prove platform authority.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from daimon.core.admin_authority import AdminAuthority, AuthorityCache
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.config import Settings


async def platform_authority_from_test_accounts(
    sessions: async_sessionmaker[AsyncSession],
    _settings: Settings,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    cache: AuthorityCache | None = None,
) -> AdminAuthority:
    del cache
    async with sessions() as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return AdminAuthority()
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform=tenant.platform, external_id=platform_user_id
        )
        account = None if principal is None else await get_account(session, principal.account_id)
    native = account is not None and account.role is Role.ADMIN
    return AdminAuthority(platform_admin=native, daimon_admin=native)
