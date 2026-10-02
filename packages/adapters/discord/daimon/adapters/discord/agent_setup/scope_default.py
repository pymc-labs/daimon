"""Adapter-tier scoped-config reads for the /agent-setup panel.

MUST NOT import daimon.core._models (ORM is private to stores/defaults). The
cross-tenant scan goes through the `list_propagations_for_tenant` store helper.
"""

from __future__ import annotations

import uuid

from daimon.core.scope import (
    ChannelConfigRow,
    TenantConfigRow,
)
from daimon.core.stores.identity import get_discord_principal_for_account
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from sqlalchemy.ext.asyncio import AsyncSession


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
