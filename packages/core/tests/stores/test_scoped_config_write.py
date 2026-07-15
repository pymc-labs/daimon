"""Tests for daimon.core.stores.scoped_config_write."""

from __future__ import annotations

import pytest
from daimon.core._models import ChannelConfig
from daimon.core.errors import StoreError
from daimon.core.scope import (
    ChannelScopeRef,
    PropagateResult,
    TenantScopeRef,
    UserScopeRef,
)
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import (
    delete_propagation_row,
    propagate,
    set_fields,
    unset_fields,
)
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_set_fields_creates_user_config_row(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="writer",
    )
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is not None
    assert row.agent_name == "writer"


async def test_set_fields_accepts_any_agent_name(
    db_session: AsyncSession,
) -> None:
    # MA is the source of truth for agents; set_fields no longer validates
    # agent_name against DB rows. Any non-empty name is accepted.
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="ghost",
    )
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is not None and row.agent_name == "ghost"


async def test_set_fields_accepts_valid_name(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="writer",
    )
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is not None and row.agent_name == "writer"


async def test_set_fields_rejects_empty_string(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    with pytest.raises(StoreError, match="empty"):
        await set_fields(
            db_session,
            scope=UserScopeRef(account_id=acct.id),
            tenant_id=t.id,
            agent_name="",
        )


async def test_set_fields_rejects_no_fields(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    with pytest.raises(StoreError, match="at least one"):
        await set_fields(
            db_session,
            scope=UserScopeRef(account_id=acct.id),
            tenant_id=t.id,
        )


async def test_set_fields_validates_account_belongs_to_tenant(
    db_session: AsyncSession,
) -> None:
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t1)
    with pytest.raises(StoreError, match="does not belong"):
        await set_fields(
            db_session,
            scope=UserScopeRef(account_id=acct.id),
            tenant_id=t2.id,
            agent_name="writer",
        )


async def test_set_fields_writes_tenant_config(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=t.id),
        tenant_id=t.id,
        agent_name="writer",
    )
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=t.id))
    assert row is not None and row.agent_name == "writer"


async def test_unset_fields_nulls_field(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="writer",
    )
    await unset_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        fields=["agent_name"],
    )
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is None


async def test_unset_fields_auto_deletes_row_when_all_null(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=t.id),
        tenant_id=t.id,
        agent_name="writer",
        environment_name="default",
    )
    await unset_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=t.id),
        fields=["agent_name"],
    )
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=t.id))
    assert row is not None
    assert row.agent_name is None
    assert row.environment_name == "default"


async def test_propagate_copies_fields_from_source_to_target(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="writer",
    )
    result = await propagate(
        db_session,
        tenant_id=t.id,
        source=UserScopeRef(account_id=acct.id),
        target=[TenantScopeRef(tenant_id=t.id)],
        fields=None,
        reset=False,
    )
    assert isinstance(result, PropagateResult)
    assert len(result.outcomes) == 1
    assert "agent_name" in result.outcomes[0].fields_written
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=t.id))
    assert row is not None and row.agent_name == "writer"


async def test_propagate_reset_clears_target(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=t.id),
        tenant_id=t.id,
        agent_name="writer",
    )
    await propagate(
        db_session,
        tenant_id=t.id,
        source=TenantScopeRef(tenant_id=t.id),
        target=[TenantScopeRef(tenant_id=t.id)],
        fields=None,
        reset=True,
    )
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=t.id))
    assert row is None


async def test_set_fields_writes_tenant_config_upsert(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    scope = TenantScopeRef(tenant_id=t.id)
    await set_fields(db_session, scope=scope, tenant_id=t.id, agent_name="writer")
    await set_fields(db_session, scope=scope, tenant_id=t.id, agent_name="writer-v2")
    row = await get_scope(db_session, scope=scope)
    assert row is not None
    assert row.agent_name == "writer-v2"


async def test_unset_fields_auto_deletes_tenant_config_row(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    scope = TenantScopeRef(tenant_id=t.id)
    await set_fields(db_session, scope=scope, tenant_id=t.id, agent_name="writer")
    await unset_fields(db_session, scope=scope, fields=["agent_name"])
    row = await get_scope(db_session, scope=scope)
    assert row is None


async def test_propagate_to_tenant_scope(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="writer",
    )
    tenant_scope = TenantScopeRef(tenant_id=t.id)
    result = await propagate(
        db_session,
        tenant_id=t.id,
        source=UserScopeRef(account_id=acct.id),
        target=[tenant_scope],
        fields=None,
        reset=False,
    )
    assert isinstance(result, PropagateResult)
    assert "agent_name" in result.outcomes[0].fields_written
    row = await get_scope(db_session, scope=tenant_scope)
    assert row is not None and row.agent_name == "writer"


async def test_propagate_skips_unset_source_fields(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    result = await propagate(
        db_session,
        tenant_id=t.id,
        source=UserScopeRef(account_id=acct.id),
        target=[TenantScopeRef(tenant_id=t.id)],
        fields=["agent_name"],
        reset=False,
    )
    assert result.outcomes[0].fields_written == []


# ---------------------------------------------------------------------------
# Audit-stamping tests
# ---------------------------------------------------------------------------


async def test_set_fields_with_actor_stamps_audit(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        agent_name="alice-bot",
        actor_account_id=acct.id,
    )
    stmt = select(ChannelConfig).where(
        ChannelConfig.tenant_id == t.id,
        ChannelConfig.channel_id == "c1",
    )
    orm = (await db_session.execute(stmt)).scalar_one()
    assert orm.agent_name == "alice-bot", "agent_name should be written"
    assert orm.agent_name_set_by_account_id == acct.id, "audit FK should equal actor_account_id"
    assert orm.agent_name_set_at is not None, "audit timestamp should be non-null"


async def test_set_fields_tenant_scope_stamps_audit(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    scope = TenantScopeRef(tenant_id=t.id)
    from daimon.core._models import TenantConfig  # noqa: PLC0415

    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        agent_name="ws-bot",
        actor_account_id=acct.id,
    )
    from sqlalchemy import select as sa_select  # noqa: PLC0415

    stmt = sa_select(TenantConfig).where(TenantConfig.tenant_id == t.id)
    orm = (await db_session.execute(stmt)).scalar_one()
    assert orm.agent_name == "ws-bot", "agent_name should be written"
    assert orm.agent_name_set_by_account_id == acct.id, (
        "tenant audit FK should equal actor_account_id"
    )
    assert orm.agent_name_set_at is not None, "tenant audit timestamp should be non-null"


async def test_set_fields_environment_only_does_not_stamp_agent_audit(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        environment_name="dev",
        actor_account_id=acct.id,
    )
    stmt = select(ChannelConfig).where(ChannelConfig.channel_id == "c1")
    orm = (await db_session.execute(stmt)).scalar_one()
    assert orm.environment_name == "dev", "environment_name should be written"
    assert orm.agent_name is None, "agent_name should remain unset"
    assert orm.agent_name_set_by_account_id is None, (
        "env-only writes must not stamp the agent-audit FK"
    )
    assert orm.agent_name_set_at is None, "env-only writes must not stamp the agent-audit timestamp"


async def test_set_fields_user_scope_does_not_attempt_audit_columns(
    db_session: AsyncSession,
) -> None:
    """UserConfig has no audit columns — writes must not crash."""
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="x",
        actor_account_id=acct.id,
    )
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is not None and row.agent_name == "x", "user-scope write should succeed"


async def test_set_fields_no_actor_stamps_null(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(db_session, scope=scope, tenant_id=t.id, agent_name="x")
    stmt = select(ChannelConfig).where(ChannelConfig.channel_id == "c1")
    orm = (await db_session.execute(stmt)).scalar_one()
    assert orm.agent_name_set_by_account_id is None, "no actor kwarg -> audit FK is NULL"
    assert orm.agent_name_set_at is not None, (
        "audit timestamp should still be stamped even without an actor"
    )


async def test_unset_fields_stamps_actor_when_row_survives(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    # Seed both agent_name and environment_name so the row survives the unset.
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        agent_name="x",
        environment_name="dev",
        actor_account_id=acct.id,
    )
    await unset_fields(
        db_session,
        scope=scope,
        fields=["agent_name"],
        actor_account_id=acct.id,
    )
    stmt = select(ChannelConfig).where(ChannelConfig.channel_id == "c1")
    orm = (await db_session.execute(stmt)).scalar_one()
    assert orm.agent_name is None, "agent_name should be NULL after unset"
    assert orm.environment_name == "dev", "environment_name should survive"
    assert orm.agent_name_set_by_account_id == acct.id, (
        "unset_fields should record the actor that cleared agent_name"
    )
    assert orm.agent_name_set_at is not None, "unset_fields should refresh the audit timestamp"


async def test_unset_fields_deletes_row_when_all_config_null(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        agent_name="x",
        actor_account_id=acct.id,
    )
    await unset_fields(
        db_session,
        scope=scope,
        fields=["agent_name"],
        actor_account_id=acct.id,
    )
    stmt = select(ChannelConfig).where(ChannelConfig.channel_id == "c1")
    result = (await db_session.execute(stmt)).scalar_one_or_none()
    assert result is None, "row should auto-delete when all config fields are NULL"


# ---------------------------------------------------------------------------
# mode column writes
# ---------------------------------------------------------------------------


async def test_set_fields_writes_mode_user_active_on_channel(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        mode="user_active",
        agent_name=None,
    )
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "channel row should be created"
    assert row.mode == "user_active", "mode should be persisted as user_active"
    assert row.agent_name is None, "agent_name remains NULL for user_active rows"


async def test_set_fields_writes_mode_agent_on_tenant(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    scope = TenantScopeRef(tenant_id=t.id)
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        mode="agent",
        agent_name="research-bot",
    )
    row = await get_scope(db_session, scope=scope)
    assert row is not None
    assert row.mode == "agent"
    assert row.agent_name == "research-bot"


async def test_set_fields_mode_silently_ignored_on_user_scope(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        mode="user_active",
        agent_name="x",
    )
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is not None, "user-scope write should succeed even when mode is supplied"
    assert row.agent_name == "x", "agent_name should still be written on user scope"


async def test_set_fields_mode_only_valid_write(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(db_session, scope=scope, tenant_id=t.id, mode="user_active")
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "mode-only write should create the row"
    assert row.mode == "user_active"


# ---------------------------------------------------------------------------
# unset_fields preserves mode='user_active' rows (Risk #1)
# ---------------------------------------------------------------------------


async def test_unset_fields_preserves_user_active_channel_row(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        mode="user_active",
        agent_name=None,
        environment_name=None,
    )
    await unset_fields(db_session, scope=scope, fields=["agent_name"])
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "user_active row must survive unset_fields"
    assert row.mode == "user_active"


async def test_unset_fields_preserves_user_active_tenant_row(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    scope = TenantScopeRef(tenant_id=t.id)
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        mode="user_active",
        agent_name=None,
        environment_name=None,
    )
    await unset_fields(db_session, scope=scope, fields=["agent_name"])
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "user_active tenant row must survive unset_fields"
    assert row.mode == "user_active"


async def test_unset_fields_still_deletes_mode_agent_row_when_empty(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(db_session, scope=scope, tenant_id=t.id, agent_name="r")
    await unset_fields(db_session, scope=scope, fields=["agent_name"])
    row = await get_scope(db_session, scope=scope)
    assert row is None, "mode='agent' row should still auto-delete when all config fields are NULL"


async def test_unset_fields_user_scope_unchanged(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    acct = await make_account(db_session, tenant=t)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=acct.id),
        tenant_id=t.id,
        agent_name="x",
    )
    await unset_fields(db_session, scope=UserScopeRef(account_id=acct.id), fields=["agent_name"])
    row = await get_scope(db_session, scope=UserScopeRef(account_id=acct.id))
    assert row is None, "user-scope row with no mode column should auto-delete as before"


async def test_unset_fields_environment_only_clear_preserves_mode_user_active_row(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        mode="user_active",
        environment_name="dev-env",
    )
    await unset_fields(db_session, scope=scope, fields=["environment_name"])
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "row should survive — mode=user_active guards it"
    assert row.mode == "user_active"
    assert row.environment_name is None


async def test_last_write_wins_records_new_actor(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    actor_a = await make_account(db_session, tenant=t)
    actor_b = await make_account(db_session, tenant=t)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        agent_name="first",
        actor_account_id=actor_a.id,
    )
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        agent_name="second",
        actor_account_id=actor_b.id,
    )
    stmt = select(ChannelConfig).where(ChannelConfig.channel_id == "c1")
    orm = (await db_session.execute(stmt)).scalar_one()
    assert orm.agent_name == "second", "last-write-wins on agent_name"
    assert orm.agent_name_set_by_account_id == actor_b.id, (
        "audit FK should reflect the second committer"
    )


async def test_delete_propagation_row_removes_user_active_channel_row(
    db_session: AsyncSession,
) -> None:
    t = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=t)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        mode="user_active",
        actor_account_id=actor.id,
    )
    deleted = await delete_propagation_row(db_session, scope=scope)
    assert deleted is True, "delete_propagation_row must report True on successful delete"
    row = await get_scope(db_session, scope=scope)
    assert row is None, "user_active channel row must be gone"


async def test_delete_propagation_row_idempotent_when_missing(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=t.id, channel_id="c1")
    deleted = await delete_propagation_row(db_session, scope=scope)
    assert deleted is False, "delete on a nonexistent row must return False, not raise"


async def test_delete_propagation_row_tenant_scope(db_session: AsyncSession) -> None:
    t = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=t)
    scope = TenantScopeRef(tenant_id=t.id)
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=t.id,
        mode="user_active",
        actor_account_id=actor.id,
    )
    deleted = await delete_propagation_row(db_session, scope=scope)
    assert deleted is True
    row = await get_scope(db_session, scope=scope)
    assert row is None, "user_active tenant row must be gone"


async def test_delete_propagation_row_does_not_touch_other_tenants(
    db_session: AsyncSession,
) -> None:
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    actor_t1 = await make_account(db_session, tenant=t1)
    actor_t2 = await make_account(db_session, tenant=t2)
    scope_t1 = TenantScopeRef(tenant_id=t1.id)
    scope_t2 = TenantScopeRef(tenant_id=t2.id)
    await set_fields(
        db_session,
        scope=scope_t1,
        tenant_id=t1.id,
        mode="user_active",
        actor_account_id=actor_t1.id,
    )
    await set_fields(
        db_session,
        scope=scope_t2,
        tenant_id=t2.id,
        mode="user_active",
        actor_account_id=actor_t2.id,
    )
    await delete_propagation_row(db_session, scope=scope_t1)
    surviving = await get_scope(db_session, scope=scope_t2)
    assert surviving is not None, "deleting t1's row must not touch t2's row"
