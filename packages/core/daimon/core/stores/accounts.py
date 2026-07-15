"""Read helper for the `accounts` table. Writes happen through
`daimon.core.stores.identity.get_or_create_cli_principal` /
`get_or_create_platform_principal`; this module only surfaces the read
path needed by the MCP verifier (account-existence check).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import Account, PlatformPrincipal, Tenant, UserConfig
from daimon.core.stores.domain import AccountIdentityRow, AccountRow, Role
from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def get_account_with_tenant(
    session: AsyncSession, *, account_id: uuid.UUID
) -> AccountIdentityRow | None:
    """Return full account identity via a single three-table JOIN.

    Joins accounts→tenants (INNER) and accounts→platform_principals (LEFT, matched on
    the tenant's own platform). Returns None when account_id does not exist. Returns
    platform_user_id=None when no PlatformPrincipal for the tenant's platform exists
    (e.g. CLI accounts, which have no PlatformPrincipal).

    The principal is matched on ``PlatformPrincipal.platform == Tenant.platform`` — not
    a fixed platform — so both discord and slack callers resolve their platform_user_id.
    A tenant is a single-platform install, so this yields exactly that platform's
    principal and never cross-matches.

    Positional unpack is used to resolve the Tenant.external_id / PlatformPrincipal.external_id
    column-name collision — never use row._mapping["external_id"].
    """
    stmt = (
        select(
            Account.id,
            Account.role,
            Account.tenant_id,
            Tenant.platform,
            Tenant.external_id,
            PlatformPrincipal.external_id,  # platform_user_id — null when no matching principal
        )
        .select_from(Account)
        .join(Tenant, Tenant.id == Account.tenant_id)
        .outerjoin(
            PlatformPrincipal,
            (PlatformPrincipal.account_id == Account.id)
            & (PlatformPrincipal.platform == Tenant.platform),
        )
        .where(Account.id == account_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    acct_id, role_str, tenant_id, platform, ext_id, platform_user_id = row
    return AccountIdentityRow(
        account_id=acct_id,
        tenant_id=tenant_id,
        role=Role(role_str),
        platform=platform,
        external_id=ext_id,
        platform_user_id=platform_user_id,
    )


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> AccountRow | None:
    orm = await session.get(Account, account_id)
    if orm is None:
        return None
    return AccountRow.model_validate(orm)


async def set_role(
    session: AsyncSession,
    account_id: uuid.UUID,
    role: Role,
) -> None:
    """Set the role on an existing account. Raises no error if account not found."""
    orm = await session.get(Account, account_id)
    if orm is None:
        return
    orm.role = role.value
    await session.flush()


async def account_exists(session: AsyncSession, *, account_id: uuid.UUID) -> bool:
    """Return True iff the accounts row exists. Read-only."""
    stmt = select(func.count()).select_from(Account).where(Account.id == account_id)
    return int((await session.execute(stmt)).scalar_one()) > 0


async def load_live_account_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Return the id of every account row. Read-only.

    Used by the MCP vault janitor to detect orphaned vaults whose owning
    account no longer exists.
    """
    rows = await session.execute(select(Account.id))
    return {row[0] for row in rows.all()}


async def count_user_config_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Count user_config rows that `delete_user_config_for_account` would delete."""
    stmt = select(func.count()).select_from(UserConfig).where(UserConfig.account_id == account_id)
    return int((await session.execute(stmt)).scalar_one())


async def delete_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Delete the account row by id. Returns rowcount; never raises on 0."""
    result = await session.execute(delete(Account).where(Account.id == account_id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def delete_user_config_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Delete the user_config row for `account_id`. Returns rowcount; never raises on 0."""
    result = await session.execute(delete(UserConfig).where(UserConfig.account_id == account_id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount
