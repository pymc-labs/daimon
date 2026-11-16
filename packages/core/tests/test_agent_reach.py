"""Shell half of agent reach: config rows and thread bindings read from the DB."""

from __future__ import annotations

import uuid

from daimon.core.agent_reach import (
    load_agent_reach,
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    delete_conversations_for_account,
    start_conversation,
)
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT = DeploymentDefault(agent_name="daimon")


async def _dm_from(
    db_session: AsyncSession, tenant: TenantRow, *, source_channel_id: str, agent_name: str
) -> uuid.UUID:
    """The rows `start_dm` writes for a /dm-me started in `source_channel_id`."""
    account = await make_account(db_session, tenant=tenant)
    await start_conversation(
        db_session,
        conversation=DirectMessageRow(
            platform="discord",
            route_key="dm1",
            external_user_id="u5",
            tenant_id=tenant.id,
            account_id=account.id,
            workspace_id="g1",
            channel_id="dm1",
            scope_id="dm:s1",
            source_url=f"https://discord.com/channels/g1/{source_channel_id}",
            source_channel_id=source_channel_id,
            context="",
            memory_read_only=False,
            history=[],
            recent_message_ids=[],
            active_until=None,
        ),
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="dm1"),
        tenant_id=tenant.id,
        agent_name=agent_name,
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="dm1",
        thread_id="dm:s1",
        responder_ma_agent_id="agent_1",
        responder_name=agent_name,
        kind="handoff",
    )
    return account.id


async def _dm_agent_channels(db_session: AsyncSession, tenant_id: uuid.UUID) -> frozenset[str]:
    reach = await load_agent_reach(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        agent_names=("dm-agent",),
        ma_agent_id=None,
        default=DEFAULT,
    )
    return reach.channel_ids


async def test_dm_origin_counts_as_its_source_channel(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account_id = await _dm_from(db_session, tenant, source_channel_id="c1", agent_name="dm-agent")

    assert await _dm_agent_channels(db_session, tenant.id) == {"c1"}
    reach = await load_agent_reach(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        agent_names=("dm-agent",),
        ma_agent_id="agent_1",
        default=DEFAULT,
    )
    assert reach.stays_inside({"c1"})
    assert not reach.stays_inside({"c2"})

    await delete_conversations_for_account(db_session, account_id=account_id)
    assert await _dm_agent_channels(db_session, tenant.id) == frozenset()


async def test_channel_and_thread_reach_stays_inside_only_its_channel(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="helper",
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c1",
        thread_id="t1",
        responder_ma_agent_id="agent_1",
        responder_name="old-name",
        kind="handoff",
    )

    async def reach():
        return await load_agent_reach(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            agent_names=("helper",),
            ma_agent_id="agent_1",
            default=DEFAULT,
        )

    first = await reach()
    assert first.channel_ids == {"c1"}
    assert first.stays_inside({"c1"})
    assert not first.stays_inside({"c2"})

    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c2",
        thread_id="t2",
        responder_ma_agent_id="agent_1",
        responder_name="old-name",
        kind="handoff",
    )
    moved = await reach()
    assert moved.channel_ids == {"c1", "c2"}
    assert not moved.stays_inside({"c1"})
    assert moved.stays_inside({"c1", "c2"})
