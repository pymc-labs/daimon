"""Per-interaction tenant resolution for /agent-setup panels and modals.

The runtime no longer carries a boot-time tenant_id; each panel/modal
callback derives its own guild's tenant id and checks it exists in tenants. Panels
gate behind ``require_registered_guild`` so a provisioned tenant is guaranteed
by the time a callback fires — a resolution miss is a bug, not a flow.
"""

from __future__ import annotations

import uuid

from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant

import discord


async def resolve_tenant_for_panel(
    runtime: DiscordRuntime, interaction: discord.Interaction
) -> uuid.UUID:
    """Resolve the interaction's guild tenant_id, per-interaction."""
    if interaction.guild_id is None:
        raise DaimonError("Panel interaction has no guild_id; cannot resolve tenant.")
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
    async with runtime.sessionmaker() as session:
        row = await get_tenant(session, tenant_id)
    if row is None:
        raise DaimonError("This server is not registered.")
    return tenant_id
