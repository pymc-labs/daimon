from __future__ import annotations

from daimon.core.stores.dm_tenant_selections import (
    count_dm_tenant_selections_for_principals,
    delete_dm_tenant_selection,
    delete_dm_tenant_selection_for_principal,
    get_dm_tenant_selection,
    set_dm_tenant_selection,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_set_dm_tenant_selection_when_repicked_then_replaces_row(
    db_session: AsyncSession,
) -> None:
    first = await make_tenant(db_session, workspace_id="guild-1")
    second = await make_tenant(db_session, workspace_id="guild-2")
    await set_dm_tenant_selection(
        db_session, platform="discord", external_user_id="u1", tenant_id=first.id
    )
    await set_dm_tenant_selection(
        db_session, platform="discord", external_user_id="u1", tenant_id=second.id
    )

    row = await get_dm_tenant_selection(db_session, platform="discord", external_user_id="u1")
    assert row is not None, "a stored pick must be readable"
    assert row.tenant_id == second.id, "a new pick must replace the old one, not add a row"


async def test_delete_dm_tenant_selection_for_principal_when_pick_names_other_tenant_then_keeps(
    db_session: AsyncSession,
) -> None:
    picked = await make_tenant(db_session, workspace_id="guild-1")
    other = await make_tenant(db_session, workspace_id="guild-2")
    await set_dm_tenant_selection(
        db_session, platform="discord", external_user_id="u1", tenant_id=picked.id
    )

    kept = await delete_dm_tenant_selection_for_principal(
        db_session, tenant_id=other.id, platform="discord", external_user_id="u1"
    )
    assert kept == 0, "purging another tenant's principal must not touch this pick"
    assert (
        await count_dm_tenant_selections_for_principals(
            db_session, principal_keys=[(picked.id, "discord", "u1")]
        )
        == 1
    ), "the preview must count what the purge path would delete"

    deleted = await delete_dm_tenant_selection_for_principal(
        db_session, tenant_id=picked.id, platform="discord", external_user_id="u1"
    )
    assert deleted == 1, "purging the principal of the picked tenant deletes the pick"


async def test_delete_dm_tenant_selection_when_absent_then_zero(db_session: AsyncSession) -> None:
    deleted = await delete_dm_tenant_selection(
        db_session, platform="discord", external_user_id="nobody"
    )
    assert deleted == 0, "forgetting a pick that was never stored is a no-op"
