"""Turn the deprecated per-workspace env DM policies into stored access.

`members` stores nothing (it is the default). `allowlist` lists those users on every agent
with DMs on. `disabled` turns the workspace feature off. Running it again changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from daimon.core.config import Settings
from daimon.core.dm_access import DmAgent
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores import dm_access as store
from daimon.core.stores.direct_messages import dm_enabled, set_dm_enabled
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession

AgentLister = Callable[[uuid.UUID], Awaitable[list[DmAgent]]]


async def import_dm_policies(
    session: AsyncSession,
    settings: Settings,
    *,
    list_agents: AgentLister,
    now: datetime,
    dry_run: bool = False,
) -> list[str]:
    """Apply each env policy as stored access; the steps taken (or, dry, that would be)."""
    steps: list[str] = []
    for tenant_id, policy in sorted(
        settings.direct_message_policies.items(), key=lambda p: str(p[0])
    ):
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or policy.mode == "members":
            continue
        if policy.mode == "disabled":
            if await dm_enabled(session, tenant_id=tenant_id):
                steps.append(f"{tenant.external_id}: turn private messages off")
                if not dry_run:
                    await set_dm_enabled(session, tenant_id=tenant_id, enabled=False)
            continue
        grants = await store.list_grants(session, tenant_id=tenant_id)
        for agent in await list_agents(tenant_id):
            place = FeaturePlace(tenant_id, tenant.platform, agent_id=agent.ma_agent_id)
            if not (await feature_in(session, settings, place, "direct_messages")).on:
                continue
            have = grants.get(agent.name)
            if have is not None and have.who == "excluded":
                steps.append(f"{tenant.external_id}: kept the exclusions on {agent.name}")
                continue
            kept = have.user_ids if have is not None and have.who == "listed" else ()
            users = tuple(dict.fromkeys((*kept, *policy.recipient_ids)))
            if have is not None and have.who == "listed" and have.user_ids == users:
                continue
            steps.append(f"{tenant.external_id}: list {len(users)} people on {agent.name}")
            if not dry_run:
                await store.set_grant(
                    session,
                    tenant_id=tenant_id,
                    agent_name=agent.name,
                    who="listed",
                    user_ids=users,
                    group_ids=have.group_ids if have is not None and have.who == "listed" else (),
                    set_by="import",
                    now=now,
                )
    return steps
