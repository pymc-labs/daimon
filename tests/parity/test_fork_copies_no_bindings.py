"""Scenario (e): fork -> no repo binding copied, on both platforms.

`driver.fork_agent` drives the REAL `tools/agents._fork_agent_impl` with this
platform's own `AuthIdentity`. A fork starts credential-less: the source's
repo binding (and with it any proof of repo access or credential ref) stays
behind, so copying an agent never hands out another project's repo access.
"""

from __future__ import annotations

import uuid
from typing import cast

from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_agent_repo_binding, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .drivers.protocol import PlatformDriver


async def test_fork_agent_copies_no_repo_binding(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(
        db_session,
        platform=cast(Platform, driver.param_id),
        workspace_id=f"fork-{driver.param_id}",
    )
    account_id = uuid.uuid4()
    await db_session.commit()

    ma_handler = make_fake_ma_handler()
    router = MARouter()
    for method in ("GET", "POST", "PATCH"):
        router.add(method, r".*", lambda req, _m, h=ma_handler: h(req))

    seed_client = build_fake_anthropic(router.dispatch)
    source = await seed_client.beta.agents.create(
        name="fork-source",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "fork-source"},
    )
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(source.id))
    await make_agent_repo_binding(
        db_session,
        tenant=tenant,
        agent_id=source_agent_uuid,
        repo_url="acme/fork-repo",
        default_branch="main",
        ma_secret_ref="anon:disabled",
    )
    await db_session.commit()

    await driver.fork_agent(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        source_name="fork-source",
        new_name="fork-target",
        account_id=account_id,
    )

    fork_ma = await find_agent_by_daimon_tag(seed_client, tenant_id=tenant.id, name="fork-target")
    assert fork_ma is not None, f"{driver.param_id}: fork_agent must create the forked MA agent"
    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(fork_ma.id))

    fork_binding = await get_binding(db_session, tenant_id=tenant.id, agent_id=fork_agent_uuid)
    assert fork_binding is None, f"{driver.param_id}: a fork must not inherit the repo binding"
