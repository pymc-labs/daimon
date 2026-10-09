"""Admin-only picture reset from the Discord setup panel."""

from __future__ import annotations

import uuid

from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_identity import identity_enabled_for, is_builtin_agent
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.stores.agent_avatars import AvatarRow, reset_avatar
from daimon.core.stores.identity import get_or_create_platform_principal

import discord

PICTURE_DISABLED = "Agent pictures are turned off."
PICTURE_NEEDS_ADMIN = "Only admins can change this agent's picture. Ask an admin to change it."


def avatar_public_url(runtime: DiscordRuntime, avatar: AvatarRow) -> str | None:
    """Use the same public image route as agent message identity."""
    base = runtime.settings.mcp.app_root_url
    if base is None:
        return None
    return f"{str(base).rstrip('/')}/avatars/{avatar.token}/{avatar.sha256[:12]}.png"


async def _audit(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: int,
    change: bool,
    outcome: PanelOutcome,
    reason: str,
    agent_name: str,
) -> None:
    await record_panel_write(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id=str(user_id),
        op="agent_avatar_change" if change else "agent_avatar_reset",
        outcome=outcome,
        reason=reason,
        agent_name=agent_name,
    )


async def _may_edit(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    change: bool,
) -> bool:
    identity_on = identity_enabled_for(runtime.settings, "discord", interaction.guild_id)
    admin = identity_on and is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
    if admin:
        agent = await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )
        if agent is not None and not is_builtin_agent(
            name=agent.name,
            metadata=agent.metadata,
            default_agent_name=runtime.deployment_default.agent_name,
        ):
            return True
    await _audit(
        runtime,
        tenant_id=tenant_id,
        user_id=interaction.user.id,
        change=change,
        outcome="denied",
        reason="needs_admin_or_agent_gone",
        agent_name=agent_name,
    )
    return False


async def reset_agent_avatar(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> tuple[str, AvatarRow | None]:
    """Restore the generated avatar with a fresh public token."""
    if not identity_enabled_for(runtime.settings, "discord", interaction.guild_id):
        await _may_edit(
            interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=False
        )
        return PICTURE_DISABLED, None
    if not await _may_edit(
        interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=False
    ):
        return PICTURE_NEEDS_ADMIN, None
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session,
            platform="discord",
            external_id=str(interaction.user.id),
            tenant_id=tenant_id,
        )
        avatar = await reset_avatar(
            session,
            tenant_id=tenant_id,
            agent_name=agent_name,
            updated_by_account_id=actor.account_id,
            face_enabled=True,
        )
    await _audit(
        runtime,
        tenant_id=tenant_id,
        user_id=interaction.user.id,
        change=False,
        outcome="allowed",
        reason="completed",
        agent_name=agent_name,
    )
    return "Default picture restored.", avatar
