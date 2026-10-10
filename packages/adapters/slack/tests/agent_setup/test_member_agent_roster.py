"""Slack member rosters follow actual local routing, including thread bindings."""

from __future__ import annotations

import pytest
from daimon.adapters.slack.agent_setup.read import load_panel_roster
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("thread", [None, "123.456"])
async def test_slack_panel_hides_unrouted_agents_and_shadowed_defaults(
    db_session: AsyncSession,
    admin: bool,
    thread: str | None,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    router = MARouter()
    router.add_agent_list(
        *[
            ma_agent(id=f"ag_{name}", name=name, tenant_id=tenant.id)
            for name in ("deployment", "workspace", "channel", "thread", "draft", "elsewhere")
        ]
    )
    await set_fields(
        db_session,
        tenant_id=tenant.id,
        scope=TenantScopeRef(tenant_id=tenant.id),
        agent_name="workspace",
    )
    for channel, name in [("CLOCAL", "channel"), ("COTHER", "elsewhere")]:
        await set_fields(
            db_session,
            tenant_id=tenant.id,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
            agent_name=name,
        )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="CLOCAL",
        thread_id="123.456",
        responder_name="thread",
        responder_ma_agent_id="ag_thread",
    )
    roster = await load_panel_roster(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        channel_id="CLOCAL",
        thread_id=thread,
        default=DeploymentDefault(agent_name="deployment"),
        is_admin=admin,
    )
    expected = {"channel"} | ({"thread"} if thread else set())
    if admin:
        expected = {"deployment", "workspace", "channel", "thread", "draft", "elsewhere"}
    assert {row.name for row in roster.rows} == expected
    assert roster.answering is not None
    assert roster.answering.name == ("thread" if thread else "channel")
