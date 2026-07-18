"""Adapter-tier scoped-config write helpers for the /agent-setup Set-as-default affordance.

MUST NOT import daimon.core._models (ORM is private to stores/defaults). Always writes
mode='agent' — the per-user 'active' tier is retired. The cross-tenant scan goes
through the `list_propagations_for_tenant` store helper.
"""

from __future__ import annotations

import dataclasses
import uuid

from daimon.core.errors import StoreError
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    ScopeRef,
    TenantConfigRow,
    TenantScopeRef,
)
from daimon.core.stores.identity import get_discord_principal_for_account
from daimon.core.stores.scoped_config_read import get_scope, list_propagations_for_tenant
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class PropagateResult:
    """What `do_propagate` returns so the caller can render an overwrite embed.

    `prior_agent_name` and `prior_actor_account_id` are the values that
    existed on the row BEFORE the write — both None on a clean propagation,
    populated on an overwrite.
    """

    prior_agent_name: str | None
    prior_actor_account_id: uuid.UUID | None


async def do_propagate(
    session: AsyncSession,
    *,
    scope: ChannelScopeRef | TenantScopeRef,
    tenant_id: uuid.UUID,
    agent_name: str | None = None,
    actor_account_id: uuid.UUID,
) -> PropagateResult:
    """Stamp agent_name at scope (mode='agent', last-write-wins).

    Returns the prior agent_name + actor so the caller can render an
    overwrite line ('replaced X → Y'). Both None on a clean write.
    """
    if not agent_name:
        raise StoreError("propagate requires agent_name")
    prior_row = await get_scope(session, scope=scope)
    prior_agent_name: str | None = None
    prior_actor: uuid.UUID | None = None
    if isinstance(prior_row, (ChannelConfigRow, TenantConfigRow)):
        prior_agent_name = prior_row.agent_name
        prior_actor = prior_row.agent_name_set_by_account_id
    await set_fields(
        session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=agent_name,
        mode="agent",
        actor_account_id=actor_account_id,
    )
    return PropagateResult(prior_agent_name=prior_agent_name, prior_actor_account_id=prior_actor)


async def do_unpropagate(
    session: AsyncSession,
    *,
    scope: ScopeRef,
    actor_account_id: uuid.UUID,
) -> None:
    """Clear agent_name at scope; the row auto-deletes if it ends fully NULL."""
    await unset_fields(
        session, scope=scope, fields=["agent_name"], actor_account_id=actor_account_id
    )


async def list_guild_propagations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> tuple[TenantConfigRow | None, list[ChannelConfigRow]]:
    """Thin adapter-tier wrapper over the core store's cross-tenant scan.

    Exists so the panel/Cog have a stable adapter-local name; the raw ORM
    query lives behind the store boundary.
    """
    return await list_propagations_for_tenant(session, tenant_id=tenant_id)


async def resolve_account_display(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> str:
    """Canonical attribution-handle resolver for audit display.

    On hit: `<@{discord_external_id}>` (renders as a Discord mention).
    On miss: `account {first8_of_uuid}`. This is the single place that joins
    audit account_id to a display string.
    """
    external_id = await get_discord_principal_for_account(session, account_id=account_id)
    if external_id is not None:
        return f"<@{external_id}>"
    return f"account {str(account_id)[:8]}"
