"""Agent helper lists against real Postgres: order, replacement and tenant scope."""

from __future__ import annotations

from daimon.core.stores import agent_helpers
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_set_helpers_replaces_the_list_and_kept_entries_keep_their_place(
    db_session: AsyncSession,
) -> None:
    """Dropped helpers go, new ones follow, and a helper kept is not re-added behind them."""
    tenant = await make_tenant(db_session)
    t = tenant.id
    await agent_helpers.set_helpers(db_session, tenant_id=t, agent_id="ag_a", helper_ids=["ag_x"])
    await agent_helpers.set_helpers(
        db_session, tenant_id=t, agent_id="ag_a", helper_ids=["ag_y", "ag_x"]
    )
    kept = await agent_helpers.list_helper_ids(db_session, tenant_id=t, agent_id="ag_a")
    assert kept == ["ag_x", "ag_y"], "the helper already listed stays first"

    await agent_helpers.set_helpers(db_session, tenant_id=t, agent_id="ag_a", helper_ids=["ag_y"])
    left = await agent_helpers.list_helper_ids(db_session, tenant_id=t, agent_id="ag_a")
    assert left == ["ag_y"], "a helper left out of the new list is removed"

    await agent_helpers.set_helpers(db_session, tenant_id=t, agent_id="ag_a", helper_ids=[])
    emptied = await agent_helpers.list_helper_ids(db_session, tenant_id=t, agent_id="ag_a")
    assert emptied == [], "an empty list clears the helpers"


async def test_set_helpers_never_lists_an_agent_as_its_own_helper(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    t = tenant.id
    await agent_helpers.set_helpers(
        db_session, tenant_id=t, agent_id="ag_a", helper_ids=["ag_a", "ag_x", "ag_x"]
    )
    stored = await agent_helpers.list_helper_ids(db_session, tenant_id=t, agent_id="ag_a")
    assert stored == ["ag_x"], "itself and repeats are not stored"


async def test_helper_lists_are_per_agent_and_per_tenant(db_session: AsyncSession) -> None:
    mine = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await agent_helpers.set_helpers(
        db_session, tenant_id=mine.id, agent_id="ag_a", helper_ids=["ag_x"]
    )
    await agent_helpers.set_helpers(
        db_session, tenant_id=mine.id, agent_id="ag_b", helper_ids=["ag_y", "ag_z"]
    )
    await agent_helpers.set_helpers(
        db_session, tenant_id=other.id, agent_id="ag_a", helper_ids=["ag_q"]
    )
    many = await agent_helpers.list_helper_ids_many(
        db_session, tenant_id=mine.id, agent_ids=["ag_a", "ag_b", "ag_c"]
    )
    assert many == {"ag_a": ["ag_x"], "ag_b": ["ag_y", "ag_z"]}, (
        "each agent's own list, none for an agent without helpers, nothing of another tenant"
    )
    mine_a = await agent_helpers.list_helper_ids(db_session, tenant_id=mine.id, agent_id="ag_a")
    assert mine_a == ["ag_x"], "another tenant's list for the same agent id is separate"
