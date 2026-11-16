"""Imported timers: erased with the person they belong to, never with someone else's."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.continuity.watch_imports import ImportedTimer
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account, purge_principal
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.held_timers import import_held_timer
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def timer(source_id: str, user: str) -> ImportedTimer:
    return ImportedTimer(
        source_id=source_id,
        owner_discord_id=user,
        channel_id="900",
        thread_id=None,
        place_type="direct-message",
        fire_at=datetime.now(UTC) + timedelta(hours=2),
        note="remember",
    )


async def hold(factory: async_sessionmaker[AsyncSession], tenant: TenantRow, user: str) -> None:
    async with factory.begin() as session:
        result = await import_held_timer(
            session, timer(f"t-{user}", user), tenant_id=tenant.id, now=datetime.now(UTC)
        )
    assert result.outcome == "held", result


async def count_rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int((await session.execute(text("select count(*) from held_timers"))).scalar_one())


async def test_erasing_an_account_removes_its_held_timers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, workspace_id="700000102")
    account = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="999", tenant=tenant, account=account
    )
    await make_platform_principal(
        db_session, platform="discord", external_id="1000", tenant=tenant, account=other
    )
    await db_session.commit()
    await hold(db_session_factory, tenant, "999")
    await hold(db_session_factory, tenant, "1000")

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    assert preview.held_timers.count == 1, "the preview names the held timer"

    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert report.db.held_timers == 1
    assert await count_rows(db_session_factory) == 1, "the other person's timer stays"


async def test_erasing_a_principal_removes_only_its_held_timers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, workspace_id="700000102")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="999", tenant=tenant
    )
    await db_session.commit()
    await hold(db_session_factory, tenant, "999")
    await hold(db_session_factory, tenant, "1000")

    report = await purge_principal(
        sm=db_session_factory, principal_id=principal.id, kind="platform"
    )

    assert report.db.held_timers == 1
    assert await count_rows(db_session_factory) == 1
