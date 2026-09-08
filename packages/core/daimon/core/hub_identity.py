"""Map a platform login to the daimon tenants and accounts it can act as.

A hub login proves one platform identity plus the set of workspaces (Slack
team, Discord guilds) that identity belongs to. Only workspaces where daimon is
installed and ready become tenants the caller can reach. For each, the caller's
account is looked up or provisioned exactly as the chat adapters do on first
contact, so a hub turn is billed and permission-checked as that person.

Ordering is by workspace name so the list a client sees is stable across
logins.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from daimon.core.stores.domain import Platform
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.tenants import list_tenants_by_platform
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, kw_only=True)
class HubTenant:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    workspace_id: str
    workspace_name: str


async def resolve_hub_tenants(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    platform: Platform,
    platform_user_id: str,
    workspaces: Sequence[tuple[str, str]],
) -> list[HubTenant]:
    """Intersect ``workspaces`` (external id, display name) with ready tenants."""
    names = dict(workspaces)
    tenants = await list_tenants_by_platform(session_factory, platform=platform)
    matched = [
        t
        for t in tenants
        if t.external_id in names and t.archived_at is None and t.provision_status == "ready"
    ]
    resolved: list[HubTenant] = []
    async with session_factory() as session, session.begin():
        for tenant in matched:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant.id, platform=platform, external_id=platform_user_id
            )
            resolved.append(
                HubTenant(
                    tenant_id=tenant.id,
                    account_id=principal.account_id,
                    workspace_id=tenant.external_id,
                    workspace_name=names[tenant.external_id],
                )
            )
    resolved.sort(key=lambda t: (t.workspace_name.lower(), t.workspace_id))
    return resolved
