import asyncio
import json
import os
from decimal import Decimal

from daimon.core.channel_backend import channel_ref
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.scope import TenantScopeRef
from daimon.core.stores import mux_state
from daimon.core.stores.access_policy import (
    load_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_write import set_fields
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def register(sf, model):
    tenant = await provision_tenant(
        sf, platform="cli", workspace_id="aks-selfhost", signup_credit=Decimal("2")
    )
    async with policy_write_transaction(sf, tenant_id=tenant.tenant_id) as s:
        policy = await load_access_policy(s, tenant_id=tenant.tenant_id)
        caller = await get_or_create_platform_principal(
            s, tenant_id=tenant.tenant_id, platform="cli", external_id="operator"
        )
        await set_fields(
            s,
            scope=TenantScopeRef(tenant_id=tenant.tenant_id),
            tenant_id=tenant.tenant_id,
            agent_name="daimon",
            environment_name="default",
        )
        await set_access_policy(
            s,
            tenant_id=tenant.tenant_id,
            policy=policy.model_copy(
                update={
                    "enabled_backends": tuple(
                        dict.fromkeys((*policy.enabled_backends, "selfhosted"))
                    )
                }
            ),
        )
        rev = ConfigRevision.create(
            channel_ref(tenant.tenant_id, "cli", "selfhosted-operator"),
            1,
            resolve_default(
                BackendConfig(
                    backend="selfhosted",
                    profile="selfhosted.loop",
                    model=model,
                )
            ),
        )
        await mux_state.put_config_revision(s, rev)
        print(
            json.dumps(
                {
                    "tenant_id": str(tenant.tenant_id),
                    "account_id": str(caller.account_id),
                    "config_digest": rev.content_digest(),
                }
            )
        )


async def main():
    e = create_async_engine(os.environ["DAIMON_DATABASE__URL"])
    try:
        await register(
            async_sessionmaker(e, expire_on_commit=False), os.environ["SELFHOSTED_MODEL"]
        )
    finally:
        await e.dispose()


if __name__ == "__main__":
    asyncio.run(main())
