"""Domain factories for Daimon test suites.

Provides cross-package factories for creating tenants, accounts, principals.
Kept as free async functions (no class, no factory_boy dependency) — factories
are called directly in tests, making setup explicit and debuggable.

These import daimon.core._models directly. daimon.testing is intentionally
excluded from the ORM contract's source_modules — factories legitimately need
ORM access for test setup.
"""

from __future__ import annotations

import uuid

from daimon.core._models import (
    Account,
    CliPrincipal,
    PlatformPrincipal,
    PrincipalLink,
    Tenant,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import Platform
from sqlalchemy.ext.asyncio import AsyncSession


async def make_tenant(
    session: AsyncSession,
    *,
    platform: Platform = "discord",
    workspace_id: str | None = None,
) -> Tenant:
    """Create a Tenant row and flush it into the session.

    WARNING: If you call make_tenant() multiple times within the same test schema
    without providing distinct workspace_id values, the UNIQUE(platform, external_id)
    constraint will raise. Pass explicit workspace_id="guild-1", workspace_id="guild-2",
    etc. when a test needs multiple tenants.

    When workspace_id is None (the default), a random UUID is used so default
    calls never collide with each other.
    """
    wid = workspace_id if workspace_id is not None else str(uuid.uuid4())
    tenant_id = derive_tenant_uuid(platform=platform, workspace_id=wid)
    tenant = Tenant(id=tenant_id, platform=platform, external_id=wid)
    session.add(tenant)
    await session.flush()
    return tenant


async def make_account(session: AsyncSession, *, tenant: Tenant | None = None) -> Account:
    tenant = tenant or await make_tenant(session)
    account = Account(tenant_id=tenant.id)
    session.add(account)
    await session.flush()
    return account


async def make_cli_principal(
    session: AsyncSession,
    *,
    os_user: str = "test",
    tenant: Tenant | None = None,
    account: Account | None = None,
) -> CliPrincipal:
    tenant = tenant or await make_tenant(session)
    account = account or await make_account(session, tenant=tenant)
    principal = CliPrincipal(tenant_id=tenant.id, os_user=os_user, account_id=account.id)
    session.add(principal)
    await session.flush()
    return principal


async def make_platform_principal(
    session: AsyncSession,
    *,
    platform: str,
    external_id: str,
    tenant: Tenant | None = None,
    account: Account | None = None,
) -> PlatformPrincipal:
    tenant = tenant or await make_tenant(session)
    account = account or await make_account(session, tenant=tenant)
    principal = PlatformPrincipal(
        tenant_id=tenant.id,
        platform=platform,
        external_id=external_id,
        account_id=account.id,
    )
    session.add(principal)
    await session.flush()
    return principal


async def link_principals(
    session: AsyncSession,
    *,
    cli: CliPrincipal,
    platform: PlatformPrincipal,
) -> PrincipalLink:
    link = PrincipalLink(
        cli_principal_id=cli.id,
        platform_principal_id=platform.id,
    )
    session.add(link)
    await session.flush()
    return link
