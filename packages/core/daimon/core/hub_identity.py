"""Map a platform login to the daimon tenants and accounts it can act as.

A hub login proves one platform identity plus the set of workspaces (Slack
team, Discord guilds) that identity belongs to. Only workspaces where daimon is
installed and ready become tenants the caller can reach; one whose member list
the chat adapter loaded answers from that list (`reached_workspaces`).
For each, the caller's account is looked up or provisioned exactly as the chat adapters do on first
contact, so a hub turn is billed and permission-checked as that person.

Ordering is by workspace name so the list a client sees is stable across
logins.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from daimon.core.platform_words import space_for
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.domain import ChatPlatform, TenantRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.tenants import list_tenants_by_platform
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Platforms whose user ids are the same person in every workspace. A Slack or Teams
#: id is only ever matched in the workspace the sign-in proves.
GLOBAL_USER_IDS: Final[frozenset[ChatPlatform]] = frozenset({"discord"})


@dataclass(frozen=True, slots=True)
class ReachedWorkspace:
    tenant: TenantRow
    name: str


def reached_workspaces(
    tenants: Sequence[TenantRow],
    *,
    platform: ChatPlatform,
    proven: Mapping[str, str],
    member_of: Collection[uuid.UUID],
    synced: Mapping[uuid.UUID, str],
) -> list[ReachedWorkspace]:
    """The ready workspaces a chat account reaches, by name.

    A workspace whose member list was loaded in full answers from that list alone, so
    a person who left loses it at once. One never loaded falls back to what the sign-in
    itself proves (`proven`: workspace id to name), so a fresh install is not locked out.
    """
    reached: list[ReachedWorkspace] = []
    for tenant in tenants:
        if tenant.platform != platform or tenant.archived_at is not None:
            continue
        if tenant.provision_status != "ready":
            continue
        if platform not in GLOBAL_USER_IDS and tenant.external_id not in proven:
            continue
        if tenant.id in synced:
            if tenant.id not in member_of:
                continue
        elif tenant.external_id not in proven:
            continue
        name = (
            proven.get(tenant.external_id)
            or synced.get(tenant.id)
            or space_for(platform).capitalize()
        )
        reached.append(ReachedWorkspace(tenant, name))
    reached.sort(key=lambda w: (w.name.lower(), w.tenant.external_id))
    return reached


@dataclass(frozen=True, kw_only=True)
class HubTenant:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    workspace_id: str
    workspace_name: str


async def resolve_hub_tenants(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    platform: ChatPlatform,
    platform_user_id: str,
    workspaces: Sequence[tuple[str, str]],
) -> list[HubTenant]:
    """The ready tenants the account reaches; ``workspaces`` (external id, display name)
    is what the login proves."""
    tenants = await list_tenants_by_platform(session_factory, platform=platform)
    tenant_ids = [t.id for t in tenants]
    resolved: list[HubTenant] = []
    async with session_factory() as session, session.begin():
        reached = reached_workspaces(
            tenants,
            platform=platform,
            proven=dict(workspaces),
            member_of=await members_store.list_member_tenant_ids(
                session, platform_user_id=platform_user_id, tenant_ids=tenant_ids
            ),
            synced=await members_store.list_synced(session, tenant_ids=tenant_ids),
        )
        for workspace in reached:
            principal = await get_or_create_platform_principal(
                session,
                tenant_id=workspace.tenant.id,
                platform=platform,
                external_id=platform_user_id,
            )
            resolved.append(
                HubTenant(
                    tenant_id=workspace.tenant.id,
                    account_id=principal.account_id,
                    workspace_id=workspace.tenant.external_id,
                    workspace_name=workspace.name,
                )
            )
    return resolved
