from __future__ import annotations

import uuid

import pytest
from daimon.core.dm_routing import (
    DmTenantCandidate,
    DmTenantSelectionRequired,
    NoDmTenantError,
    choose_dm_tenant,
    forget_dm_tenant,
    pick_dm_tenant,
    resolve_dm_tenant,
)
from daimon.core.stores.tenants import set_provision_status
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _candidate(workspace_id: str) -> DmTenantCandidate:
    return DmTenantCandidate(tenant_id=uuid.uuid4(), workspace_id=workspace_id)


def test_pick_dm_tenant_when_one_candidate_then_returns_it_without_a_pick() -> None:
    only = _candidate("guild-1")
    assert pick_dm_tenant((only,), selected=None) == only, "one tenant needs no picker"


def test_pick_dm_tenant_when_several_and_stale_pick_then_asks() -> None:
    candidates = (_candidate("guild-1"), _candidate("guild-2"))
    with pytest.raises(DmTenantSelectionRequired) as raised:
        pick_dm_tenant(candidates, selected=uuid.uuid4())
    assert raised.value.candidates == candidates, "the picker lists every live candidate"


def test_pick_dm_tenant_when_no_candidates_then_raises() -> None:
    with pytest.raises(NoDmTenantError):
        pick_dm_tenant((), selected=None)


async def test_resolve_dm_tenant_when_one_live_tenant_then_returns_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    live = await make_tenant(db_session, workspace_id="guild-1")
    archived = await make_tenant(db_session, workspace_id="guild-2")
    await db_session.commit()
    await set_provision_status(db_session_factory, tenant_id=archived.id, archive=True)

    resolved = await resolve_dm_tenant(
        db_session_factory,
        platform="discord",
        external_user_id="u1",
        workspace_ids=["guild-1", "guild-2", "guild-unknown"],
    )
    assert resolved.tenant_id == live.id, "archived and unknown workspaces are not candidates"


async def test_resolve_dm_tenant_when_several_then_asks_until_chosen_and_after_forget(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_tenant(db_session, workspace_id="guild-1")
    second = await make_tenant(db_session, workspace_id="guild-2")
    await db_session.commit()
    workspaces = ["guild-1", "guild-2"]

    with pytest.raises(DmTenantSelectionRequired) as raised:
        await resolve_dm_tenant(
            db_session_factory, platform="discord", external_user_id="u1", workspace_ids=workspaces
        )
    assert [c.workspace_id for c in raised.value.candidates] == workspaces, (
        "candidates keep the adapter's order"
    )

    await choose_dm_tenant(
        db_session_factory,
        platform="discord",
        external_user_id="u1",
        workspace_ids=workspaces,
        tenant_id=second.id,
    )
    resolved = await resolve_dm_tenant(
        db_session_factory, platform="discord", external_user_id="u1", workspace_ids=workspaces
    )
    assert resolved.tenant_id == second.id, "a stored pick routes later DMs without asking"

    assert await forget_dm_tenant(db_session_factory, platform="discord", external_user_id="u1")
    with pytest.raises(DmTenantSelectionRequired):
        await resolve_dm_tenant(
            db_session_factory, platform="discord", external_user_id="u1", workspace_ids=workspaces
        )


async def test_choose_dm_tenant_when_tenant_not_a_candidate_then_asks_again(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_tenant(db_session, workspace_id="guild-1")
    await make_tenant(db_session, workspace_id="guild-2")
    await db_session.commit()

    with pytest.raises(DmTenantSelectionRequired):
        await choose_dm_tenant(
            db_session_factory,
            platform="discord",
            external_user_id="u1",
            workspace_ids=["guild-1", "guild-2"],
            tenant_id=uuid.uuid4(),
        )
