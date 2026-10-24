"""The shared-channel registry and the sticky foreign marker on principals."""

import pytest
from daimon.core.stores.identity import get_or_create_platform_principal, mark_principal_foreign
from daimon.core.stores.shared_channels import is_channel_shared, mark_channel_shared
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


async def test_an_unrecorded_channel_is_not_shared_and_a_record_is_per_tenant_and_platform(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C_SHARED", source="client"
    )
    assert await is_channel_shared(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C_SHARED"
    )
    assert not await is_channel_shared(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C_OTHER"
    )
    assert not await is_channel_shared(
        db_session, tenant_id=tenant.id, platform="teams", channel_id="C_SHARED"
    )
    assert not await is_channel_shared(
        db_session, tenant_id=other.id, platform="slack", channel_id="C_SHARED"
    )


async def test_marking_a_channel_twice_is_idempotent_and_keeps_the_first_source(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for source in ("platform", "client", "platform"):
        await mark_channel_shared(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C_SHARED",
            source=source,  # pyright: ignore[reportArgumentType]
        )
    rows = (await db_session.execute(text("SELECT source FROM shared_channels"))).all()
    assert [row[0] for row in rows] == ["platform"]


async def test_the_registry_refuses_an_unknown_source(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(DBAPIError):
        await mark_channel_shared(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C_SHARED",
            source="guess",  # pyright: ignore[reportArgumentType]
        )


async def test_the_foreign_marker_is_written_once_and_never_overwritten_or_cleared(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_EXTERNAL"
    )
    assert principal.foreign_home_id is None

    await mark_principal_foreign(db_session, principal_id=principal.id, home_id="T_OTHER")
    await mark_principal_foreign(db_session, principal_id=principal.id, home_id="T_ELSEWHERE")

    again = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_EXTERNAL"
    )
    assert again.foreign_home_id == "T_OTHER"
