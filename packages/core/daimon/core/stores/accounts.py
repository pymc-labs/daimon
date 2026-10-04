"""Read helper for the `accounts` table. Writes happen through
`daimon.core.stores.identity.get_or_create_cli_principal` /
`get_or_create_platform_principal`; this module only surfaces the read
path needed by the MCP verifier (account-existence check).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from typing import Any, cast

from daimon.core._models import Account, PlatformPrincipal, Tenant, UserConfig
from daimon.core.stores.domain import AccountIdentityRow, AccountRow, Role
from sqlalchemy import ARRAY, Text, any_, bindparam, case, delete, func, or_, select, update
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
            Account.platform_role_ids,
            Account.is_external,
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
    acct_id, role_str, tenant_id, platform, ext_id, platform_user_id, role_ids, external = row
    return AccountIdentityRow(
        account_id=acct_id,
        tenant_id=tenant_id,
        role=Role(role_str),
        platform=platform,
        external_id=ext_id,
        platform_user_id=platform_user_id,
        platform_role_ids=tuple(role_ids),
        is_external=external,
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
    """Set the role on an existing account. Raises no error if account not found.

    An external account stays a user whatever is asked: no path makes one an
    admin. One UPDATE, so it reads `is_external` as committed when it runs.
    """
    effective_role = case((Account.is_external, Role.USER.value), else_=role.value)
    await session.execute(
        update(Account)
        .where(Account.id == account_id, Account.role.is_distinct_from(effective_role))
        .values(role=effective_role)
    )


async def get_external(session: AsyncSession, account_id: uuid.UUID) -> bool:
    """Whether the account is known to be from another organisation; False if it is gone."""
    stored = await session.scalar(select(Account.is_external).where(Account.id == account_id))
    return stored is True


async def set_external(session: AsyncSession, account_id: uuid.UUID, is_external: bool) -> None:
    """Record positive evidence that the account is from another organisation, or ours.

    Marking demotes it to user. One UPDATE that writes only a change, so
    concurrent turns with the same evidence never contend or undo each other.
    """
    demote = {"role": Role.USER.value} if is_external else {}
    await session.execute(
        update(Account)
        .where(Account.id == account_id, Account.is_external.is_not(is_external))
        .values(is_external=is_external, **demote)
    )


async def set_platform_role_ids(
    session: AsyncSession,
    account_id: uuid.UUID,
    role_ids: Sequence[str],
) -> None:
    """Replace the account's stored platform role ids. No error if the account is gone."""
    orm = await session.get(Account, account_id)
    if orm is None:
        return
    normalized = sorted(set(role_ids))
    if orm.platform_role_ids != normalized:
        orm.platform_role_ids = normalized
        await session.flush()


async def demote_unlisted_admins(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, admin_ids: Collection[str]
) -> int:
    """Demote every `platform` admin in `tenant_id` whose external id is not in `admin_ids`.

    For a platform whose admins are configuration: the live role is written
    only on a person's next turn, and routines and MCP clients act on the
    stored one until then. Returns how many accounts were demoted.
    """
    unlisted = select(PlatformPrincipal.account_id).where(
        PlatformPrincipal.tenant_id == tenant_id,
        PlatformPrincipal.platform == platform,
        PlatformPrincipal.external_id.not_in(admin_ids),
    )
    stmt = (
        update(Account)
        .where(Account.id.in_(unlisted), Account.role == Role.ADMIN.value)
        .values(role=Role.USER.value)
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    return result.rowcount


async def key_share_lock_account(session: AsyncSession, *, account_id: uuid.UUID) -> None:
    """Hold the account row FOR KEY SHARE to the end of the caller's transaction.

    Taken before any row that references the account, so an account purge
    (which takes the row FOR UPDATE first) is waited for here rather than
    while this transaction holds those rows.
    """
    await session.execute(
        select(Account.id)
        .where(Account.id == account_id)
        .with_for_update(read=True, key_share=True)
    )


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


async def list_platform_user_ids(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    limit: int | None,
    admins: bool = False,
    user_ids: Collection[str] = (),
    role_ids: Collection[str] = (),
    among: Collection[str] | None = None,
    fold_case: bool = False,
) -> list[str]:
    """Platform user ids of the tenant's stored admins (`admins`), or of the listed
    users plus anyone whose stored roles overlap `role_ids`; sorted, at most `limit`
    (None: all). `among`, if given, keeps only those ids, compared lower-cased with
    `fold_case`; it binds as one array, so a large group fits in one query.
    Accounts from another organisation are never listed: no admin message
    reaches them."""
    if admins:
        match = Account.role == Role.ADMIN.value
    else:
        match = or_(
            PlatformPrincipal.external_id.in_(list(user_ids)),
            Account.platform_role_ids.overlap(list(role_ids)),
        )
    stmt = (
        select(PlatformPrincipal.external_id)
        .join(Account, Account.id == PlatformPrincipal.account_id)
        .where(PlatformPrincipal.tenant_id == tenant_id, PlatformPrincipal.platform == platform)
        .where(match, Account.is_external.is_(False))
        .order_by(PlatformPrincipal.external_id)
        .limit(limit)
    )
    if among is not None:
        external_id = PlatformPrincipal.external_id
        column = func.lower(external_id) if fold_case else external_id
        wanted = [uid.lower() if fold_case else uid for uid in among]
        stmt = stmt.where(column == any_(bindparam("among", wanted, type_=ARRAY(Text))))
    return list((await session.scalars(stmt)).all())


async def list_external_platform_user_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, user_ids: Collection[str]
) -> set[str]:
    """Which of `user_ids` belong to accounts held as from another organisation."""
    stmt = (
        select(PlatformPrincipal.external_id)
        .join(Account, Account.id == PlatformPrincipal.account_id)
        .where(PlatformPrincipal.tenant_id == tenant_id, PlatformPrincipal.platform == platform)
        .where(PlatformPrincipal.external_id.in_(list(user_ids)), Account.is_external.is_(True))
    )
    return set((await session.scalars(stmt)).all())
