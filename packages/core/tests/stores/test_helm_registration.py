"""Exercise the chart registration script against the real policy store."""

import runpy
from pathlib import Path

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.access_policy import (
    load_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_register_twice_preserves_existing_access_controls(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await provision_tenant(db_session_factory, platform="cli", workspace_id="aks-selfhost")
    policy = TenantAccessPolicy(
        invoker_user_ids=("restricted-user",),
        protected_channel_ids=("protected-channel",),
        sealed_channel_ids=("sealed-channel",),
        protected_category_ids=("protected-category",),
        agent_channel_pins={"daimon": ("pinned-channel",)},
        dm_memory_read_only=True,
        enabled_backends=("anthropic", "openai"),
    )
    async with policy_write_transaction(db_session_factory, tenant_id=tenant.tenant_id) as session:
        await set_access_policy(session, tenant_id=tenant.tenant_id, policy=policy)
    script = (
        Path(__file__).resolve().parents[4] / "deploy/helm/daimon-selfhost/scripts/provision.py"
    )
    register = runpy.run_path(str(script))["register"]
    for _ in range(2):
        await register(db_session_factory, "gpt-5.4-mini")
        async with db_session_factory() as session:
            actual = await load_access_policy(session, tenant_id=tenant.tenant_id)
        assert actual == policy.model_copy(
            update={"enabled_backends": ("anthropic", "openai", "selfhosted")}
        )
