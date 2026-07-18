"""Tests for daimon.core.stores.scoped_config_read."""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.core._models import ChannelConfig, TenantConfig, UserConfig
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
    TenantScopeRef,
    UserScopeRef,
)
from daimon.core.stores.scoped_config_read import (
    get_scope,
    list_propagations_for_tenant,
    resolve,
)
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_DEFAULT = DeploymentDefault(agent_name="daimon", environment_name="default")


async def test_resolve_returns_all_none_when_no_config_exists(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    ctx = ScopeContext(tenant_id=t.id)
    result = await resolve(db_session, context=ctx, default=DeploymentDefault())
    assert result.agent_name is None
    assert result.agent_name_tier is None


async def test_resolve_no_rows_returns_deployment_default(db_session: AsyncSession) -> None:
    """THE SPINE INVARIANT: a fresh tenant with zero config rows resolves to the injected default."""
    t = await make_tenant(db_session)
    result = await resolve(
        db_session,
        context=ScopeContext(tenant_id=t.id),
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert result.agent_name == "daimon", (
        "resolve() with no rows must return agent_name from the injected DeploymentDefault"
    )
    assert result.environment_name == "default", (
        "resolve() with no rows must return environment_name from the injected DeploymentDefault"
    )


async def test_resolve_returns_tenant_as_fallback(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    await make_account(db_session, tenant=t)
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="writer", environment_name="default"))
    await db_session.flush()
    ctx = ScopeContext(tenant_id=t.id)
    result = await resolve(db_session, context=ctx, default=DeploymentDefault())
    assert result.agent_name == "writer"
    assert result.agent_name_tier == "tenant"


async def test_resolve_three_tier_with_channel(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    await make_account(db_session, tenant=t)
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="writer", environment_name="default"))
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name="channel-bot",
        )
    )
    await db_session.flush()
    ctx = ScopeContext(tenant_id=t.id, channel_id="c1")
    result = await resolve(db_session, context=ctx, default=DeploymentDefault())
    assert result.agent_name == "channel-bot"
    assert result.agent_name_tier == "channel"
    assert result.environment_name == "default"


async def test_resolve_channel_wins_over_tenant(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    await make_account(db_session, tenant=t)
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="writer", environment_name="default"))
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name="channel-bot",
            environment_name="channel-env",
        )
    )
    await db_session.flush()
    ctx = ScopeContext(tenant_id=t.id, channel_id="c1")
    result = await resolve(db_session, context=ctx, default=DeploymentDefault())
    assert result.agent_name == "channel-bot", "channel wins over tenant"
    assert result.agent_name_tier == "channel", "tier should be channel"
    assert result.environment_name == "channel-env", "channel wins for environment"
    assert result.environment_name_tier == "channel", "tier should be channel"


async def test_resolve_skips_channel_tier_when_no_channel_id(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    await make_account(db_session, tenant=t)
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="writer"))
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name="channel-bot",
        )
    )
    await db_session.flush()
    ctx = ScopeContext(tenant_id=t.id)
    result = await resolve(db_session, context=ctx, default=DeploymentDefault())
    assert result.agent_name == "writer"


async def test_get_scope_returns_user_row(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    db_session.add(UserConfig(account_id=acct.id, agent_name="my-agent"))
    await db_session.flush()
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is not None
    assert row.agent_name == "my-agent"


async def test_get_scope_returns_channel_row(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name="bot",
        )
    )
    await db_session.flush()
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    row = await get_scope(db_session, scope=scope)
    assert row is not None
    assert row.agent_name == "bot"


async def test_get_scope_returns_tenant_row(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="writer"))
    await db_session.flush()
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=t.id))
    assert row is not None
    assert row.agent_name == "writer"


async def test_get_scope_returns_none_when_absent(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=t.id))
    assert row is None


async def test_get_scope_channel_row_round_trips_audit_columns(
    db_session: AsyncSession,
) -> None:
    """ChannelConfigRow exposes agent_name_set_by_account_id + agent_name_set_at
    when the underlying row has them populated."""
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    pinned_at = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name="bot",
            agent_name_set_by_account_id=acct.id,
            agent_name_set_at=pinned_at,
        )
    )
    await db_session.flush()
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "channel row should be returned after insert"
    assert row.agent_name == "bot"
    assert getattr(row, "agent_name_set_by_account_id", None) == acct.id, (
        "ChannelConfigRow should expose the actor account id"
    )
    assert getattr(row, "agent_name_set_at", None) == pinned_at, (
        "ChannelConfigRow should expose the audit timestamp"
    )


async def test_get_scope_channel_row_audit_columns_default_none(
    db_session: AsyncSession,
) -> None:
    """When audit columns are NULL in the DB, the Pydantic row exposes None for both
    — no KeyError, no validation failure."""
    t = await make_tenant(db_session)
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name="bot",
        )
    )
    await db_session.flush()
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    row = await get_scope(db_session, scope=scope)
    assert row is not None
    assert getattr(row, "agent_name_set_by_account_id", "missing") is None
    assert getattr(row, "agent_name_set_at", "missing") is None


# ---------------------------------------------------------------------------
# list_propagations_for_tenant tests
# ---------------------------------------------------------------------------


async def test_list_propagations_for_tenant_returns_tenant_and_channels(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    pinned = datetime(2026, 5, 13, 9, 0, 0, tzinfo=UTC)
    db_session.add(
        TenantConfig(
            tenant_id=t.id,
            agent_name="guild-bot",
            agent_name_set_by_account_id=acct.id,
            agent_name_set_at=pinned,
        )
    )
    # Seed three channel rows out of order; helper must sort by channel_id asc.
    for cid in ("c3", "c1", "c2"):
        db_session.add(
            ChannelConfig(
                tenant_id=t.id,
                channel_id=cid,
                agent_name=f"bot-{cid}",
                agent_name_set_by_account_id=acct.id,
                agent_name_set_at=pinned,
            )
        )
    await db_session.flush()

    tenant_row, ch_rows = await list_propagations_for_tenant(db_session, tenant_id=t.id)
    assert tenant_row is not None, "tenant row should be returned"
    assert tenant_row.agent_name == "guild-bot"
    assert tenant_row.agent_name_set_by_account_id == acct.id, "tenant audit FK should round-trip"
    assert tenant_row.agent_name_set_at == pinned, "tenant audit timestamp should round-trip"
    assert [r.agent_name for r in ch_rows] == [
        "bot-c1",
        "bot-c2",
        "bot-c3",
    ], "channels must be sorted by channel_id ascending"
    assert all(r.agent_name_set_by_account_id == acct.id for r in ch_rows), (
        "each channel row should expose the audit FK"
    )


async def test_list_propagations_for_tenant_filters_by_tenant(
    db_session: AsyncSession,
) -> None:
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    # Rows in the target tenant
    db_session.add(TenantConfig(tenant_id=t1.id, agent_name="target-tenant"))
    db_session.add(ChannelConfig(tenant_id=t1.id, channel_id="c1", agent_name="target-ch"))
    # Noise: different tenant
    db_session.add(ChannelConfig(tenant_id=t2.id, channel_id="c1", agent_name="other-tenant"))
    await db_session.flush()

    tenant_row, ch_rows = await list_propagations_for_tenant(db_session, tenant_id=t1.id)
    assert tenant_row is not None and tenant_row.agent_name == "target-tenant"
    assert [r.agent_name for r in ch_rows] == ["target-ch"], (
        "channel results must filter by tenant_id"
    )


async def test_list_propagations_for_tenant_returns_none_tenant_when_absent(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    db_session.add(ChannelConfig(tenant_id=t.id, channel_id="c1", agent_name="ch-only"))
    await db_session.flush()
    tenant_row, ch_rows = await list_propagations_for_tenant(db_session, tenant_id=t.id)
    assert tenant_row is None, "no tenant row -> tenant_row should be None"
    assert len(ch_rows) == 1, "channel rows should still be returned"


async def test_list_propagations_for_tenant_returns_empty_when_nothing_seeded(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    tenant_row, ch_rows = await list_propagations_for_tenant(db_session, tenant_id=t.id)
    assert tenant_row is None, "empty DB -> tenant_row is None"
    assert ch_rows == [], "empty DB -> channel list is empty"


async def test_list_propagations_for_tenant_returns_pydantic_not_orm(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="ws"))
    db_session.add(ChannelConfig(tenant_id=t.id, channel_id="c1", agent_name="ch"))
    await db_session.flush()
    tenant_row, ch_rows = await list_propagations_for_tenant(db_session, tenant_id=t.id)
    assert isinstance(tenant_row, TenantConfigRow), (
        "tenant_row should be the Pydantic row, not the ORM"
    )
    assert all(isinstance(r, ChannelConfigRow) for r in ch_rows), (
        "each channel result should be the Pydantic row, not the ORM"
    )


# ---------------------------------------------------------------------------
# mode-aware resolve + list_propagations mode round-trip
# ---------------------------------------------------------------------------


async def test_resolve_user_active_channel_falls_through_to_tenant(
    db_session: AsyncSession,
) -> None:
    """A channel with mode='user_active' is skipped by the resolver (no substitution
    path remains); the cascade falls through to tenant."""
    t = await make_tenant(db_session)
    await make_account(db_session, tenant=t)
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name=None,
            mode="user_active",
        )
    )
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="daimon"))
    await db_session.flush()
    ctx = ScopeContext(tenant_id=t.id, channel_id="c1")
    result = await resolve(db_session, context=ctx, default=DeploymentDefault())
    assert result.agent_name == "daimon", (
        "user_active channel row has no agent_name; cascade falls through to tenant"
    )
    assert result.agent_name_tier == "tenant", "tier should be tenant"


async def test_resolve_channel_agent_mode_resolves_agent_name(
    db_session: AsyncSession,
) -> None:
    """Channel with mode='agent' resolves its agent_name directly."""
    t = await make_tenant(db_session)
    await make_account(db_session, tenant=t)
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name="c",
            mode="agent",
        )
    )
    await db_session.flush()
    ctx = ScopeContext(tenant_id=t.id, channel_id="c1")
    result = await resolve(db_session, context=ctx, default=DeploymentDefault())
    assert result.agent_name == "c", "mode='agent' channel resolves its agent_name"
    assert result.agent_name_tier == "channel", "tier should be channel"


async def test_list_propagations_for_tenant_returns_mode(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    db_session.add(TenantConfig(tenant_id=t.id, agent_name="ws", mode="agent"))
    db_session.add(
        ChannelConfig(
            tenant_id=t.id,
            channel_id="c1",
            agent_name=None,
            mode="user_active",
        )
    )
    await db_session.flush()
    tenant_row, ch_rows = await list_propagations_for_tenant(db_session, tenant_id=t.id)
    assert tenant_row is not None and tenant_row.mode == "agent", (
        "tenant row should expose mode='agent'"
    )
    assert len(ch_rows) == 1 and ch_rows[0].mode == "user_active", (
        "channel row should expose mode='user_active'"
    )


# ---------------------------------------------------------------------------
# Invariant tests
# ---------------------------------------------------------------------------


async def test_cross_tenant_isolation(db_session: AsyncSession) -> None:
    """Tenant A's config reads never touch tenant B.

    Pins threat T-58.3-01: cross-tenant config isolation.
    """
    from daimon.core.stores.scoped_config_write import set_fields  # noqa: PLC0415

    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)

    # Write a tenant_config row for tenant A only
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_a.id),
        tenant_id=tenant_a.id,
        agent_name="agent-for-a",
    )
    await db_session.flush()

    default = DeploymentDefault(agent_name="daimon", environment_name="default")

    # Resolving tenant B must return the deployment default, never tenant A's agent_name
    result_b = await resolve(
        db_session,
        context=ScopeContext(tenant_id=tenant_b.id),
        default=default,
    )
    assert result_b.agent_name == "daimon", (
        "resolving tenant B must return the deployment default, not tenant A's config"
    )
    assert result_b.agent_name != "agent-for-a", (
        "tenant B must never see tenant A's agent_name — cross-tenant isolation violated"
    )

    # Resolving tenant A must return its own value
    result_a = await resolve(
        db_session,
        context=ScopeContext(tenant_id=tenant_a.id),
        default=default,
    )
    assert result_a.agent_name == "agent-for-a", (
        "resolving tenant A must return its own tenant_config agent_name"
    )
