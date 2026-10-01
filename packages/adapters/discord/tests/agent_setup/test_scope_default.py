"""Real-Postgres write-path tests for agent_setup/scope_default.py.

Re-homed from tests/propagate/test_write.py.
The behavioral coverage is preserved; per-user-tier tests are dropped
(The per-user tier is retired; the fold only ever writes
mode="agent", which is implicit and has no mode= kwarg in the new API).
"""

from __future__ import annotations

import pytest
from daimon.adapters.discord.agent_setup.scope_default import (
    PropagateResult,
    do_propagate,
    do_unpropagate,
    list_guild_propagations,
)
from daimon.core.errors import StoreError
from daimon.core.scope import ChannelScopeRef, TenantScopeRef
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Write-path behavioral coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_propagate_clean_scope_returns_no_prior_and_stamps_audit(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=tenant)
    scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="c1")
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        actor_account_id=actor.id,
    )
    assert result == PropagateResult(
        prior_agent_name=None,
        prior_actor_account_id=None,
    ), "clean propagate must return both prior values as None"
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "a row must be created after do_propagate"
    assert row.agent_name == "writer-v1", "row should hold the propagated agent_name"
    assert getattr(row, "agent_name_set_by_account_id", None) == actor.id, (
        "audit column must record the actor account_id"
    )


@pytest.mark.asyncio
async def test_do_propagate_overwrite_returns_prior_values(
    db_session: AsyncSession,
) -> None:
    """Overwrite returns prior agent name for cascade naming."""
    tenant = await make_tenant(db_session)
    actor_a = await make_account(db_session, tenant=tenant)
    actor_b = await make_account(db_session, tenant=tenant)
    scope = TenantScopeRef(tenant_id=tenant.id)
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        actor_account_id=actor_a.id,
    )
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v2",
        actor_account_id=actor_b.id,
    )
    assert result.prior_agent_name == "writer-v1", (
        "overwrite must capture prior agent_name for the 'replaced X → Y' line"
    )
    assert result.prior_actor_account_id == actor_a.id, (
        "overwrite must capture prior actor_account_id"
    )
    row = await get_scope(db_session, scope=scope)
    assert row is not None and row.agent_name == "writer-v2", (
        "row must reflect the new agent_name after overwrite"
    )
    assert getattr(row, "agent_name_set_by_account_id", None) == actor_b.id, (
        "audit column must be re-stamped to the new actor"
    )


@pytest.mark.asyncio
async def test_do_unpropagate_deletes_row_when_only_agent_name_was_set(
    db_session: AsyncSession,
) -> None:
    """Clearing agent_name auto-deletes fully-NULL row."""
    tenant = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=tenant)
    scope = TenantScopeRef(tenant_id=tenant.id)
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        actor_account_id=actor.id,
    )
    await do_unpropagate(db_session, scope=scope, actor_account_id=actor.id)
    row = await get_scope(db_session, scope=scope)
    assert row is None, "row must be deleted when both agent_name and environment_name end up NULL"


@pytest.mark.asyncio
async def test_do_unpropagate_preserves_row_when_environment_name_still_set(
    db_session: AsyncSession,
) -> None:
    """Clearing agent_name preserves the row when environment_name is set."""
    tenant = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=tenant)
    scope = TenantScopeRef(tenant_id=tenant.id)
    # set both fields explicitly via set_fields, then unpropagate only agent_name
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        environment_name="prod",
        actor_account_id=actor.id,
    )
    await do_unpropagate(db_session, scope=scope, actor_account_id=actor.id)
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "row must survive when environment_name is still set"
    assert row.agent_name is None, "agent_name must be cleared by do_unpropagate"
    assert row.environment_name == "prod", "environment_name must be untouched by do_unpropagate"


@pytest.mark.asyncio
async def test_list_guild_propagations_filters_by_tenant_id(
    db_session: AsyncSession,
) -> None:
    """list_guild_propagations must isolate by tenant_id."""
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    actor_a = await make_account(db_session, tenant=tenant_a)
    actor_b = await make_account(db_session, tenant=tenant_b)
    # tenant_a: tenant-level + 2 channels
    await do_propagate(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_a.id),
        tenant_id=tenant_a.id,
        agent_name="tenant-bot",
        actor_account_id=actor_a.id,
    )
    await do_propagate(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_a.id, channel_id="c1"),
        tenant_id=tenant_a.id,
        agent_name="c1-bot",
        actor_account_id=actor_a.id,
    )
    await do_propagate(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_a.id, channel_id="c2"),
        tenant_id=tenant_a.id,
        agent_name="c2-bot",
        actor_account_id=actor_a.id,
    )
    # tenant_b — must NOT leak across tenants
    await do_propagate(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_b.id),
        tenant_id=tenant_b.id,
        agent_name="other-tenant",
        actor_account_id=actor_b.id,
    )
    tenant_row, ch_rows = await list_guild_propagations(db_session, tenant_id=tenant_a.id)
    assert tenant_row is not None and tenant_row.agent_name == "tenant-bot", (
        "tenant row must be returned for the target tenant"
    )
    channel_names = sorted(r.agent_name for r in ch_rows if r.agent_name is not None)
    assert channel_names == ["c1-bot", "c2-bot"], (
        "only channel rows for the target tenant must be returned"
    )


@pytest.mark.asyncio
async def test_do_propagate_requires_agent_name(db_session: AsyncSession) -> None:
    """do_propagate must raise StoreError on falsy agent_name (mode='agent' is implicit)."""
    tenant = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=tenant)
    scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="c1")
    with pytest.raises(StoreError, match="agent_name"):
        await do_propagate(
            db_session,
            scope=scope,
            tenant_id=tenant.id,
            agent_name=None,
            actor_account_id=actor.id,
        )
