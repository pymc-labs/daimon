"""Real-Postgres tests for agent_setup/scope_default.py."""

from __future__ import annotations

from daimon.adapters.discord.agent_setup.scope_default import list_guild_propagations
from daimon.core.scope import ChannelScopeRef, TenantScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_list_guild_propagations_filters_by_tenant_id(
    db_session: AsyncSession,
) -> None:
    """list_guild_propagations must isolate by tenant_id."""
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    actor_a = await make_account(db_session, tenant=tenant_a)
    actor_b = await make_account(db_session, tenant=tenant_b)
    # tenant_a: tenant-level + 2 channels
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_a.id),
        tenant_id=tenant_a.id,
        agent_name="tenant-bot",
        mode="agent",
        actor_account_id=actor_a.id,
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_a.id, channel_id="c1"),
        tenant_id=tenant_a.id,
        agent_name="c1-bot",
        mode="agent",
        actor_account_id=actor_a.id,
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_a.id, channel_id="c2"),
        tenant_id=tenant_a.id,
        agent_name="c2-bot",
        mode="agent",
        actor_account_id=actor_a.id,
    )
    # tenant_b — must NOT leak across tenants
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_b.id),
        tenant_id=tenant_b.id,
        agent_name="other-tenant",
        mode="agent",
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
