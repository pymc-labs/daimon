"""A Space decides the same on Discord, Slack and Teams, through each real entry point.

One own Space of two channels whose agent names both: a turn in either member (in a
thread under it on Discord) is answered, one in a third channel is not. Teams' private and shared channels, read
only from inside them, are covered in `packages/adapters/mcp/tests/tools/test_teams_read.py`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.stores import spaces, tenant_ledger
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import DEFAULT_AGENT_NAME
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AGENT_TEXT, build_turn_router
from .drivers.protocol import PlatformDriver, platform_ids

OWN = ChannelRule(readers="own", writers="own")


@pytest.mark.parametrize(("channel", "answered"), [(1, True), (2, True), (3, False)])
async def test_a_space_agent_answers_in_its_members_only(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    channel: int,
    answered: bool,
) -> None:
    ids = [
        platform_ids(driver.param_id, workspace=900004001, user=555000114, channel=n)
        for n in (11, 21, 31)
    ]
    workspace_id, user_id, _ = ids[0]
    # Discord's driver posts in a thread under channel n - 1, so that is the member.
    discord = driver.param_id == "discord"
    members = tuple(str(int(c) - 1) if discord else c for _, _, c in ids[:2])
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    policy = TenantAccessPolicy(
        channel_rules=dict.fromkeys(members, OWN),
        agent_rules={DEFAULT_AGENT_NAME: AgentRule(runs_in=members)},
    )
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    space = await spaces.create_space(db_session, tenant_id=tenant.id, name="s", rule=OWN)
    for member in members:
        await spaces.add_space_channel(
            db_session, space=space, tenant_id=tenant.id, channel_id=member
        )
    await db_session.commit()

    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(str(tenant.id)),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=ids[channel - 1][2],
        user_id=user_id,
        text="hello",
    )

    assert any(AGENT_TEXT in body for body in posted) is answered, f"{driver.param_id}: {posted}"
