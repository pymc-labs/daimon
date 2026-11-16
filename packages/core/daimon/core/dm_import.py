"""Turn the deprecated per-workspace env DM policies into stored access.

`members` stores nothing (it is the default). `allowlist` lists those users on every agent:
agent-sent messages are held to the sending agent's grant whether or not it takes DMs.
`disabled` turns the workspace feature off. Each write is recorded for the CLI's local
user, like any other CLI change. Running it again changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from daimon.core import dm_settings
from daimon.core.config import Settings
from daimon.core.dm_access import DmAgent
from daimon.core.dm_settings import DmActor
from daimon.core.stores import dm_access as store
from daimon.core.stores.direct_messages import dm_enabled, set_dm_enabled
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession

AgentLister = Callable[[uuid.UUID], Awaitable[list[DmAgent]]]


async def import_dm_policies(
    session: AsyncSession,
    settings: Settings,
    *,
    agents_of: AgentLister,
    os_user: str,
    now: datetime,
    dry_run: bool = False,
) -> list[str]:
    """Apply each env policy as stored access; the steps taken (or, dry, that would be)."""

    async def actor(tenant_id: uuid.UUID) -> DmActor:
        principal = await get_or_create_cli_principal(session, tenant_id=tenant_id, os_user=os_user)
        return DmActor(tenant_id, principal.account_id, now)

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
                    by = (await actor(tenant_id)).account_id
                    await set_dm_enabled(session, tenant_id=tenant_id, enabled=False, account_id=by)
            continue
        grants = await store.list_grants(session, tenant_id=tenant_id)
        for agent in await agents_of(tenant_id):
            have = grants.get(agent.name)
            if have is not None and have.who == "excluded":
                steps.append(f"{tenant.external_id}: kept the exclusions on {agent.name}")
                continue
            listed = have if have is not None and have.who == "listed" else None
            kept = () if listed is None else listed.user_ids
            users = tuple(dict.fromkeys((*kept, *policy.recipient_ids)))
            if listed is not None and listed.user_ids == users:
                continue
            steps.append(f"{tenant.external_id}: list {len(users)} people on {agent.name}")
            if not dry_run:
                await dm_settings.set_grant(
                    session,
                    await actor(tenant_id),
                    agent,
                    agent.name,
                    who="listed",
                    user_ids=users,
                    group_ids=() if listed is None else listed.group_ids,
                )
    return steps
