from __future__ import annotations

from daimon.core._models import Account, CliPrincipal, PlatformPrincipal, PrincipalLink
from daimon.testing.factories import (
    link_principals,
    make_account,
    make_cli_principal,
    make_platform_principal,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_db_session_search_path_points_at_the_worker_schema(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(text("SHOW search_path"))
    path = result.scalar_one()
    assert path.startswith("test_") or "test_" in path, (
        f"search_path should target this worker's test_w<pid>_<hex> schema, got {path!r}"
    )


async def test_make_account_inserts_and_assigns_uuid_when_called(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    assert account.id is not None, "account id must be populated by server default"

    rows = (await db_session.execute(select(Account))).scalars().all()
    assert len(rows) == 1, "the per-test wipe should leave zero rows before this test inserts"


async def test_principal_factories_roundtrip_when_linked(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    cli = await make_cli_principal(db_session, os_user="tester", account=account)
    platform = await make_platform_principal(
        db_session, platform="discord", external_id="12345", account=account
    )
    link = await link_principals(db_session, cli=cli, platform=platform)

    assert link.cli_principal_id == cli.id
    assert link.platform_principal_id == platform.id

    cli_rows = (await db_session.execute(select(CliPrincipal))).scalars().all()
    platform_rows = (await db_session.execute(select(PlatformPrincipal))).scalars().all()
    link_rows = (await db_session.execute(select(PrincipalLink))).scalars().all()
    assert len(cli_rows) == 1
    assert len(platform_rows) == 1
    assert len(link_rows) == 1


async def test_worker_schema_is_wiped_before_each_test_regardless_of_prior_test_state(
    db_session: AsyncSession,
) -> None:
    """Proves the per-test wipe of the shared worker schema — no rows bleed across tests."""
    rows = (await db_session.execute(select(Account))).scalars().all()
    assert rows == [], "worker schema should be wiped before each test, prior test leaked"
