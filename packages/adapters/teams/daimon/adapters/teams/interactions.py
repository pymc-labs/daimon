"""Who clicked: the checks every card action and dialog runs before acting.

Invokes skip `parse_inbound`, so each handler re-verifies the organisation
and the clicker's Entra id here, then gets the tenant and admin role.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.adapters.teams.identity import canonical_uuid
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant
from microsoft_teams.api import ConversationAccount


@dataclass(frozen=True)
class Actor:
    """A verified clicker in a live tenant."""

    user_id: str
    tenant_id: uuid.UUID
    is_admin: bool
    conversation_id: str


async def resolve_actor(
    runtime: TeamsRuntime, *, conversation: ConversationAccount, aad_object_id: str | None
) -> Actor | None:
    """The clicker, or None when the org, the id or the tenant does not check out."""
    teams = runtime.settings.teams
    if teams is None or canonical_uuid(conversation.tenant_id) != teams.tenant_id:
        return None
    user_id = canonical_uuid(aad_object_id)
    if user_id is None:
        return None
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=teams.tenant_id)
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.provision_status != "ready" or tenant.archived_at is not None:
        return None
    return Actor(
        user_id=user_id,
        tenant_id=tenant_id,
        is_admin=user_id in teams.admin_user_ids,
        conversation_id=conversation.id,
    )
