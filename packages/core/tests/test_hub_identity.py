"""resolve_hub_tenants intersects a user's workspaces with installed tenants."""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.core.hub_identity import resolve_hub_tenants
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.tenants import set_provision_status
from daimon.testing.factories import make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_returns_only_workspaces_with_a_ready_tenant(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    installed = await make_tenant(db_session, platform="discord", workspace_id="g-installed")
    await make_tenant(db_session, platform="slack", workspace_id="g-other-platform")
    await db_session.commit()

    tenants = await resolve_hub_tenants(
        db_session_factory,
        platform="discord",
        platform_user_id="u1",
        workspaces=[
            ("g-installed", "Installed"),
            ("g-missing", "Missing"),
            ("g-other-platform", "X"),
        ],
    )

    assert [t.tenant_id for t in tenants] == [installed.id], f"got {tenants!r}"
    assert tenants[0].workspace_name == "Installed", f"got {tenants[0].workspace_name!r}"


async def test_provisions_an_account_on_first_contact_and_reuses_it_after(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()

    first = await resolve_hub_tenants(
        db_session_factory, platform="discord", platform_user_id="u1", workspaces=[("g1", "G")]
    )
    second = await resolve_hub_tenants(
        db_session_factory, platform="discord", platform_user_id="u1", workspaces=[("g1", "G")]
    )

    assert first[0].account_id == second[0].account_id, "second login must reuse the account"
    principal = await find_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u1"
    )
    assert principal is not None and principal.account_id == first[0].account_id, (
        f"principal row must point at the resolved account, got {principal!r}"
    )


async def test_reuses_existing_principal_account(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    principal = await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant
    )
    await db_session.commit()

    tenants = await resolve_hub_tenants(
        db_session_factory, platform="slack", platform_user_id="U1", workspaces=[("T1", "Acme")]
    )
    assert tenants[0].account_id == principal.account_id, (
        f"must resolve to the pre-existing account, got {tenants[0].account_id}"
    )


async def test_skips_archived_and_non_ready_tenants(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    pending = await make_tenant(db_session, platform="discord", workspace_id="g-pending")
    archived = await make_tenant(db_session, platform="discord", workspace_id="g-archived")
    await db_session.commit()
    await set_provision_status(db_session_factory, tenant_id=pending.id, status="pending")
    await set_provision_status(
        db_session_factory, tenant_id=archived.id, status="ready", archive=True
    )

    tenants = await resolve_hub_tenants(
        db_session_factory,
        platform="discord",
        platform_user_id="u1",
        workspaces=[("g-pending", "P"), ("g-archived", "A")],
    )
    assert tenants == [], (
        f"neither a non-ready nor an archived tenant may be offered, got {tenants!r}"
    )


async def test_a_loaded_member_list_decides_where_a_discord_account_reaches(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A list loaded in full overrides the login's guilds both ways; no list falls back."""
    listed = await make_tenant(db_session, platform="discord", workspace_id="g-listed")
    left = await make_tenant(db_session, platform="discord", workspace_id="g-left")
    fresh = await make_tenant(db_session, platform="discord", workspace_id="g-fresh")
    now = datetime(2026, 5, 1, tzinfo=UTC)
    await members_store.replace_members(
        db_session, tenant_id=listed.id, members={"u1": False}, workspace_name="Listed", now=now
    )
    await members_store.replace_members(
        db_session, tenant_id=left.id, members={}, workspace_name="Left", now=now
    )
    await db_session.commit()

    tenants = await resolve_hub_tenants(
        db_session_factory,
        platform="discord",
        platform_user_id="u1",
        workspaces=[("g-left", "Left"), ("g-fresh", "Fresh")],
    )

    assert {t.tenant_id: t.workspace_name for t in tenants} == {
        listed.id: "Listed",
        fresh.id: "Fresh",
    }, f"got {tenants!r}"
