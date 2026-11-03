"""Identity store — accounts, CLI/platform principals, and link lifecycle."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, Literal, cast

from daimon.core._models import (
    Account,
    CliPrincipal,
    PlatformPrincipal,
    PrincipalLink,
)
from daimon.core.stores.domain import (
    CliPrincipalRow,
    PlatformPrincipalRow,
)
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement


async def get_or_create_cli_principal(
    session: AsyncSession, *, tenant_id: uuid.UUID, os_user: str
) -> CliPrincipalRow:
    """Look up the CLI principal for `os_user`; create account + principal if absent."""
    existing = await session.execute(
        select(CliPrincipal).where(
            CliPrincipal.tenant_id == tenant_id,
            CliPrincipal.os_user == os_user,
        )
    )
    orm = existing.scalar_one_or_none()
    if orm is not None:
        return CliPrincipalRow.model_validate(orm)

    account = Account(tenant_id=tenant_id, role="user")
    session.add(account)
    await session.flush()

    principal = CliPrincipal(tenant_id=tenant_id, os_user=os_user, account_id=account.id)
    session.add(principal)
    await session.flush()
    return CliPrincipalRow.model_validate(principal)


async def get_or_create_platform_principal(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, external_id: str
) -> PlatformPrincipalRow:
    """Reuse the tenant's principal, including when another transaction creates it."""
    lookup = select(PlatformPrincipal).where(
        PlatformPrincipal.tenant_id == tenant_id,
        PlatformPrincipal.platform == platform,
        PlatformPrincipal.external_id == external_id,
    )
    orm = (await session.execute(lookup)).scalar_one_or_none()
    if orm is not None:
        return PlatformPrincipalRow.model_validate(orm)

    # A concurrent enrollment can be invisible to the SELECT yet win the unique
    # key. Roll back its losing account too, without touching the caller's work.
    async with session.begin_nested() as savepoint:
        account = Account(tenant_id=tenant_id, role="user")
        session.add(account)
        await session.flush()
        orm = (
            await session.execute(
                insert(PlatformPrincipal)
                .values(
                    tenant_id=tenant_id,
                    platform=platform,
                    external_id=external_id,
                    account_id=account.id,
                )
                .on_conflict_do_nothing(constraint="uq_platform_principal")
                .returning(PlatformPrincipal)
            )
        ).scalar_one_or_none()
        if orm is None:
            await savepoint.rollback()

    if orm is None:
        # READ COMMITTED gives this statement a fresh snapshot of the winner.
        # Only the named uniqueness conflict is ignored; other errors propagate.
        orm = (await session.execute(lookup)).scalar_one()
    return PlatformPrincipalRow.model_validate(orm)


async def create_platform_principal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_id: str,
    account_id: uuid.UUID,
) -> PlatformPrincipalRow:
    """Record `(platform, external_id)` as an existing account's.

    For a platform id proven by sign-in before the platform ever saw it, so its first
    message there runs as the account that signed in.
    """
    principal = PlatformPrincipal(
        tenant_id=tenant_id, platform=platform, external_id=external_id, account_id=account_id
    )
    session.add(principal)
    await session.flush()
    return PlatformPrincipalRow.model_validate(principal)


async def mark_principal_foreign(
    session: AsyncSession, *, principal_id: uuid.UUID, home_id: str
) -> None:
    """Record the home organisation of a principal verified to belong to another one.

    Sticky: set only while unset, and never cleared by code, so a later lookup
    that cannot tell cannot turn a client back into a member.
    """
    await session.execute(
        update(PlatformPrincipal)
        .where(PlatformPrincipal.id == principal_id, PlatformPrincipal.foreign_home_id.is_(None))
        .values(foreign_home_id=home_id)
    )


async def find_platform_principal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_id: str,
) -> PlatformPrincipalRow | None:
    """Look up `(tenant_id, platform, external_id)`. Returns None on miss — NEVER creates.

    Read-only counterpart of `get_or_create_platform_principal`. Used by surfaces
    (notably /privacy) that need to ask "does this platform user have any daimon
    state?" without minting an Account row as a side effect.
    """
    stmt = select(PlatformPrincipal).where(
        PlatformPrincipal.tenant_id == tenant_id,
        PlatformPrincipal.platform == platform,
        PlatformPrincipal.external_id == external_id,
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else PlatformPrincipalRow.model_validate(orm)


async def delete_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    kind: Literal["cli", "platform"],
) -> int:
    """Delete the principal row by id. Returns rowcount; never raises on 0."""
    Model = CliPrincipal if kind == "cli" else PlatformPrincipal
    result = await session.execute(delete(Model).where(Model.id == principal_id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def delete_principal_links_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    kind: Literal["cli", "platform"],
) -> int:
    """Delete all principal_links rows referencing this principal on the named side.

    Returns rowcount; never raises on 0 (idempotency contract).
    """
    col = PrincipalLink.cli_principal_id if kind == "cli" else PrincipalLink.platform_principal_id
    result = await session.execute(delete(PrincipalLink).where(col == principal_id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_principal_links_for_account(
    session: AsyncSession,
    *,
    cli_principal_ids: list[uuid.UUID],
    platform_principal_ids: list[uuid.UUID],
) -> int:
    """Count distinct principal_link rows touching any of the given principal ids.

    Mirrors what `purge_account` deletes: each link involving any CLI or platform
    principal under the account is deleted exactly once, regardless of how many
    of its endpoints fall in the input lists. Used by the /privacy cascade preview.
    """
    if not cli_principal_ids and not platform_principal_ids:
        return 0
    conditions: list[ColumnElement[bool]] = []
    if cli_principal_ids:
        conditions.append(PrincipalLink.cli_principal_id.in_(cli_principal_ids))
    if platform_principal_ids:
        conditions.append(PrincipalLink.platform_principal_id.in_(platform_principal_ids))
    stmt = select(func.count()).select_from(PrincipalLink).where(or_(*conditions))
    return int((await session.execute(stmt)).scalar_one())


async def get_principal_by_id(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    kind: Literal["cli", "platform"],
) -> CliPrincipalRow | PlatformPrincipalRow | None:
    """Look up a principal by id and kind. Returns None if absent.

    Used by the GDPR purge orchestrator to resolve a `(principal_id, kind)`
    request to the row needed by `_purge_principal_in_session`. Lives here
    (not in `purge.py`) to keep ORM imports out of the orchestrator.
    """
    if kind == "cli":
        cli_stmt = select(CliPrincipal).where(CliPrincipal.id == principal_id)
        cli_orm = (await session.execute(cli_stmt)).scalar_one_or_none()
        return CliPrincipalRow.model_validate(cli_orm) if cli_orm is not None else None
    pp_stmt = select(PlatformPrincipal).where(PlatformPrincipal.id == principal_id)
    pp_orm = (await session.execute(pp_stmt)).scalar_one_or_none()
    return PlatformPrincipalRow.model_validate(pp_orm) if pp_orm is not None else None


async def list_cli_principals_for_account(
    session: AsyncSession, *, account_id: uuid.UUID
) -> list[CliPrincipalRow]:
    stmt = select(CliPrincipal).where(CliPrincipal.account_id == account_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [CliPrincipalRow.model_validate(r) for r in rows]


async def list_platform_principals_for_account(
    session: AsyncSession, *, account_id: uuid.UUID
) -> list[PlatformPrincipalRow]:
    stmt = select(PlatformPrincipal).where(PlatformPrincipal.account_id == account_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [PlatformPrincipalRow.model_validate(r) for r in rows]


async def list_principals_by_external_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, external_ids: Collection[str]
) -> list[PlatformPrincipalRow]:
    """The tenant's platform principals whose external id is one of `external_ids`."""
    if not external_ids:
        return []
    stmt = select(PlatformPrincipal).where(
        PlatformPrincipal.tenant_id == tenant_id,
        PlatformPrincipal.external_id.in_(list(external_ids)),
    )
    return [PlatformPrincipalRow.model_validate(r) for r in (await session.execute(stmt)).scalars()]


async def get_discord_principal_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> str | None:
    """Return the Discord external_id (platform_user_id) for this account, or None.

    Mirror of list_platform_principals_for_account but scalar + filtered to
    platform='discord'. Used by IdentityMiddleware to populate
    AuthIdentity.platform_user_id without changing JWT shape.
    """
    stmt = select(PlatformPrincipal.external_id).where(
        PlatformPrincipal.account_id == account_id,
        PlatformPrincipal.platform == "discord",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_slack_principal_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> str | None:
    """Return the Slack external_id (platform user id) for this account, or None.

    Slack analog of get_discord_principal_for_account — used by the /agent-setup
    audit-display resolver to render <@U…> for scope-propagation audit rows.
    """
    stmt = select(PlatformPrincipal.external_id).where(
        PlatformPrincipal.account_id == account_id,
        PlatformPrincipal.platform == "slack",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_teams_principal_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> str | None:
    """Return the Teams external_id (the Entra object id) for this account, or None.

    Teams analog of get_slack_principal_for_account, for naming who set an
    agent on the /here card.
    """
    stmt = select(PlatformPrincipal.external_id).where(
        PlatformPrincipal.account_id == account_id,
        PlatformPrincipal.platform == "teams",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def platform_external_ids(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    account_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Each account's user id on `platform` in this tenant; accounts with none are left out."""
    if not account_ids:
        return {}
    rows = await session.execute(
        select(PlatformPrincipal.account_id, PlatformPrincipal.external_id).where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.account_id.in_(list(account_ids)),
        )
    )
    return {account_id: external_id for account_id, external_id in rows.all()}
