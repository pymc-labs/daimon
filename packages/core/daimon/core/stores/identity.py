"""Identity store — accounts, CLI/platform principals, and link lifecycle."""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from daimon.core._models import (
    Account,
    CliPrincipal,
    PlatformPrincipal,
    PrincipalLink,
)
from daimon.core.errors import StoreError
from daimon.core.stores.domain import (
    CliPrincipalRow,
    PlatformPrincipalRow,
    PrincipalLinkRow,
)
from sqlalchemy import delete, func, or_, select
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
    """Look up `(platform, external_id)`; create account + principal if absent."""
    existing = await session.execute(
        select(PlatformPrincipal).where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == external_id,
        )
    )
    orm = existing.scalar_one_or_none()
    if orm is not None:
        return PlatformPrincipalRow.model_validate(orm)

    account = Account(tenant_id=tenant_id, role="user")
    session.add(account)
    await session.flush()

    principal = PlatformPrincipal(
        tenant_id=tenant_id, platform=platform, external_id=external_id, account_id=account.id
    )
    session.add(principal)
    await session.flush()
    return PlatformPrincipalRow.model_validate(principal)


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


async def set_active_agent_name(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    agent_name: str | None,
) -> None:
    """Set or clear the per-principal active agent (daimon-tag string).

    Storage is the daimon-tag (not the MA UUID) so the value survives MA archive
    cycles the same way routines do. Empty string is normalized to None.
    Calling with an unknown principal_id is a no-op (idempotency contract,
    same shape as `delete_for_principal`).
    """
    normalized = agent_name if agent_name else None
    orm = await session.get(PlatformPrincipal, principal_id)
    if orm is None:
        return
    orm.active_agent_name = normalized
    await session.flush()


async def create_principal_link(
    session: AsyncSession,
    *,
    cli_principal_id: uuid.UUID,
    platform_principal_id: uuid.UUID,
) -> PrincipalLinkRow:
    """Insert the `(cli, platform)` edge. Caller validates existence upstream."""
    link = PrincipalLink(
        cli_principal_id=cli_principal_id,
        platform_principal_id=platform_principal_id,
    )
    session.add(link)
    await session.flush()
    return PrincipalLinkRow.model_validate(link)


async def list_links_for_cli(
    session: AsyncSession, *, cli_principal_id: uuid.UUID
) -> list[PrincipalLinkRow]:
    stmt = select(PrincipalLink).where(PrincipalLink.cli_principal_id == cli_principal_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [PrincipalLinkRow.model_validate(r) for r in rows]


async def delete_principal_link(
    session: AsyncSession,
    *,
    cli_principal_id: uuid.UUID,
    platform_principal_id: uuid.UUID,
) -> None:
    stmt = delete(PrincipalLink).where(
        PrincipalLink.cli_principal_id == cli_principal_id,
        PrincipalLink.platform_principal_id == platform_principal_id,
    )
    result = await session.execute(stmt)
    if cast(CursorResult[Any], result).rowcount == 0:
        raise StoreError(
            f"no link between cli_principal={cli_principal_id} and "
            f"platform_principal={platform_principal_id}"
        )
    await session.flush()


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


async def count_principal_links_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    kind: Literal["cli", "platform"],
) -> int:
    """Count principal_link rows that `delete_principal_links_for_principal` would delete."""
    col = PrincipalLink.cli_principal_id if kind == "cli" else PrincipalLink.platform_principal_id
    stmt = select(func.count()).select_from(PrincipalLink).where(col == principal_id)
    return int((await session.execute(stmt)).scalar_one())


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


async def resolve_linked_platform_principal(
    session: AsyncSession,
    *,
    cli_principal_id: uuid.UUID,
    platform: str,
    external_id: str,
) -> PlatformPrincipalRow | None:
    """Return the platform principal iff `cli_principal_id` has a link to it.

    This is the authoritative check for `--as <platform>:<external_id>` — no
    link, no impersonation.
    """
    stmt = (
        select(PlatformPrincipal)
        .join(
            PrincipalLink,
            PrincipalLink.platform_principal_id == PlatformPrincipal.id,
        )
        .where(
            PrincipalLink.cli_principal_id == cli_principal_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == external_id,
        )
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    if orm is None:
        return None
    return PlatformPrincipalRow.model_validate(orm)
