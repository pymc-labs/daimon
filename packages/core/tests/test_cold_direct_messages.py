"""Cold scopes bind an explicit agent without channel context or routing writes."""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.direct_messages import admit_cold_dm, start_cold_dm
from daimon.core.scope import ChannelScopeRef, ResolvedConfig
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.direct_messages import (
    claim_message,
    finish_message,
    get_conversation,
    get_dm_agent_name,
    set_dm_agent_name,
    set_dm_enabled,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.turn.admission import Admission, reauthorize
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
    make_thread_session,
)
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


async def seed(factory):
    async with factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="700000101")
        account = await make_account(session, tenant=tenant)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="private")
    admission = Admission(
        account_id=account.id,
        agent=ma_agent(id="ag_private", name="private", tenant_id=tenant.id),
        environment=ma_environment(id="env_1", name="default", tenant_id=tenant.id),
        config=ResolvedConfig(agent_name="private", environment_name="default"),
    )
    return tenant, admission, deps_for(None, factory)


async def test_cold_start_serializes_absent_route_and_pins_scope_only(db_engine, db_clean):
    db_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant, admission, deps = await seed(db_session_factory)

    async def start():
        return await start_cold_dm(
            deps,
            admission,
            tenant_id=tenant.id,
            workspace_id=tenant.external_id,
            route_key="555",
            external_user_id="999",
        )

    rows = await asyncio.gather(start(), start())
    assert sum(row is not None for row in rows) == 1
    row = next(row for row in rows if row is not None)
    assert row.context == "" and row.history == [] and row.source_channel_id is None
    assert row.source_thread_id is None and row.source_thread_keys is None
    async with db_session_factory() as session:
        config = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="555")
        )
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="555",
            thread_id=row.scope_id,
        )
    assert config is None, "cold starts never write physical-channel configuration"
    assert binding.responder_ma_agent_id == "ag_private"


async def test_switch_retires_session_and_stale_switch_cannot_replace_scope(db_session_factory):
    tenant, admission, deps = await seed(db_session_factory)
    old = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key="555",
        external_user_id="999",
    )
    async with db_session_factory.begin() as session:
        mapping = await make_thread_session(
            session, tenant=tenant, thread_id=old.scope_id, ma_session_id="old_private_session"
        )
    new = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key="555",
        external_user_id="999",
        expected_scope_id=old.scope_id,
    )
    assert new.scope_id != old.scope_id and new.history == []
    async with db_session_factory() as session:
        retired = await get_thread_session_by_id(session, id=mapping.id)
    assert retired.status == "dead"
    assert (
        await start_cold_dm(
            deps,
            admission,
            tenant_id=tenant.id,
            workspace_id=tenant.external_id,
            route_key="555",
            external_user_id="999",
            expected_scope_id=old.scope_id,
        )
        is None
    )


async def test_duplicate_message_claims_and_routine_delivery_do_not_change_mapping(
    db_session_factory,
):
    tenant, admission, deps = await seed(db_session_factory)
    row = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key="555",
        external_user_id="999",
    )
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        first = await claim_message(
            session,
            platform="discord",
            route_key="555",
            external_user_id="999",
            message_id="101",
            expected_scope_id=row.scope_id,
            now=now,
            active_until=now,
        )
        await finish_message(session, conversation=first, history=[{"role": "user", "text": "hi"}])
    async with db_session_factory.begin() as session:
        assert (
            await claim_message(
                session,
                platform="discord",
                route_key="555",
                external_user_id="999",
                message_id="101",
                expected_scope_id=row.scope_id,
                now=now,
                active_until=now,
            )
            is None
        )
    # A routine poster only uses a transport; querying the route is read-only.
    async with db_session_factory() as session:
        active = await get_conversation(
            session, platform="discord", route_key="555", external_user_id="999"
        )
    assert active.scope_id == row.scope_id
    assert active.history == [{"role": "user", "text": "hi"}]


@pytest.mark.parametrize("restricted", ["pin", "isolated"])
async def test_cold_agent_restrictions_apply_to_admins(db_session_factory, restricted):
    tenant, admission, deps = await seed(db_session_factory)
    async with db_session_factory.begin() as session:
        await make_tenant_config(
            session, tenant=tenant, agent_name="different", environment_name="default"
        )
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
        policy = (
            TenantAccessPolicy(agent_rules={"private": AgentRule(runs_in=("123",))})
            if restricted == "pin"
            else TenantAccessPolicy(
                channel_rules={"123": ChannelRule(readers="own", writers="own")},
                agent_rules={"private": AgentRule(runs_in=("123",))},
            )
        )
        await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    client = build_fake_anthropic(
        resolved_agent_env_router(admission.agent, admission.environment).dispatch
    )
    with pytest.raises(AdmissionDenied):
        await admit_cold_dm(
            replace(deps, anthropic=client),
            tenant_id=tenant.id,
            external_user_id="999",
            channel_id="555",
            role=Role.ADMIN,
            role_ids=(),
            verified_membership=True,
        )
    await client.close()


async def test_read_only_change_is_rechecked_before_session_use(db_session_factory):
    tenant, fake_admission, deps = await seed(db_session_factory)
    async with db_session_factory.begin() as session:
        await make_tenant_config(
            session, tenant=tenant, agent_name="different", environment_name="default"
        )
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
    client = build_fake_anthropic(
        resolved_agent_env_router(fake_admission.agent, fake_admission.environment).dispatch
    )
    deps = replace(deps, anthropic=client)
    admission = await admit_cold_dm(
        deps,
        tenant_id=tenant.id,
        external_user_id="999",
        channel_id="555",
        role=Role.USER,
        role_ids=(),
        verified_membership=True,
    )
    assert admission.config.agent_name == "private"
    assert not admission.memory_read_only
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session, tenant_id=tenant.id, policy=TenantAccessPolicy(dm_memory_read_only=True)
        )
    assert (await reauthorize(deps, admission)).memory_read_only
    await client.close()


@pytest.mark.fresh_schema
async def test_dm_agent_migration_round_trip_preserves_enablement(db_session):
    path = Path(__file__).parents[1] / "alembic/versions/0134_cold_dm_agent.py"
    spec = importlib.util.spec_from_file_location("cold_dm_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    tenant = await make_tenant(db_session)
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await set_dm_agent_name(db_session, tenant_id=tenant.id, agent_name="private")
    connection = await db_session.connection()

    def run(step):
        def apply(conn):
            with Operations.context(MigrationContext.configure(conn)):
                getattr(migration, step)()

        return apply

    await connection.run_sync(run("downgrade"))
    assert (
        await db_session.scalar(
            text("SELECT enabled FROM direct_message_policies WHERE tenant_id=:id"),
            {"id": tenant.id},
        )
        is True
    )
    await connection.run_sync(run("upgrade"))
    assert await get_dm_agent_name(db_session, tenant_id=tenant.id) is None


async def test_missing_configured_agent_is_not_provisioned_or_replaced(db_session_factory):
    from daimon.core.ma_resolver import MAResolverMissError

    tenant, admission, deps = await seed(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="missing")
        await make_tenant_config(
            session, tenant=tenant, agent_name="private", environment_name="default"
        )
    router = resolved_agent_env_router(admission.agent, admission.environment)
    client = build_fake_anthropic(router.dispatch)
    with pytest.raises(MAResolverMissError):
        await admit_cold_dm(
            replace(deps, anthropic=client),
            tenant_id=tenant.id,
            external_user_id="999",
            channel_id="555",
            role=Role.USER,
            role_ids=(),
            verified_membership=True,
        )
    async with db_session_factory() as session:
        assert await get_dm_agent_name(session, tenant_id=tenant.id) == "missing"
    await client.close()


async def test_cold_provenance_does_not_inherit_unrelated_channel_seals(db_session_factory):
    from daimon.core.direct_messages import _source_is_sealed

    tenant, admission, deps = await seed(db_session_factory)
    row = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key="555",
        external_user_id="999",
    )
    policy = TenantAccessPolicy(channel_rules={"123": ChannelRule(readers="inside")})
    assert not _source_is_sealed(policy, row)
    legacy = row.model_copy(update={"source_url": "https://example.com/source"})
    assert _source_is_sealed(policy, legacy), "legacy moves still fail closed without provenance"


async def test_changed_account_never_reuses_private_session(db_session_factory, monkeypatch):
    from unittest.mock import AsyncMock

    from daimon.core import direct_messages
    from daimon.core.direct_messages import DirectMessageAccessRevoked, reply_to_dm

    tenant, admission, deps = await seed(db_session_factory)
    row = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key="555",
        external_user_id="999",
    )
    async with db_session_factory.begin() as session:
        replacement = await make_account(session, tenant=tenant)
    monkeypatch.setattr(
        direct_messages,
        "admit",
        AsyncMock(return_value=replace(admission, account_id=replacement.id)),
    )
    bind = AsyncMock()
    monkeypatch.setattr(direct_messages, "bind_session", bind)
    with pytest.raises(DirectMessageAccessRevoked, match="account changed"):
        await reply_to_dm(
            deps,
            platform="discord",
            route_key="555",
            external_user_id="999",
            message_id="101",
            expected_scope_id=row.scope_id,
            text="hi",
            role=Role.USER,
            verified_membership=True,
        )
    bind.assert_not_awaited()


async def test_disabling_dms_after_admission_prevents_session_use(db_session_factory):
    tenant, fake_admission, deps = await seed(db_session_factory)
    async with db_session_factory.begin() as session:
        await make_tenant_config(
            session, tenant=tenant, agent_name="private", environment_name="default"
        )
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
    client = build_fake_anthropic(
        resolved_agent_env_router(fake_admission.agent, fake_admission.environment).dispatch
    )
    deps = replace(deps, anthropic=client)
    admission = await admit_cold_dm(
        deps,
        tenant_id=tenant.id,
        external_user_id="999",
        channel_id="555",
        role=Role.USER,
        role_ids=(),
        verified_membership=True,
    )
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=False)
    with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
        await reauthorize(deps, admission)
    await client.close()
