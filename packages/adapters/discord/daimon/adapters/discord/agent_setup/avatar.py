"""Admin-only avatar writes from the Discord setup panel and slash command."""

from __future__ import annotations

import asyncio
import uuid

from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_avatar_image import MAX_UPLOAD_BYTES, normalize_avatar_image
from daimon.core.agent_identity import is_builtin_agent
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.stores.agent_avatars import AvatarRow, replace_avatar, reset_avatar
from daimon.core.stores.identity import get_or_create_platform_principal

import discord


def avatar_public_url(runtime: DiscordRuntime, avatar: AvatarRow) -> str | None:
    """Use the same public image route as agent message identity."""
    base = runtime.settings.mcp.public_url
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
) -> None:
    await record_panel_write(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id=str(user_id),
        op="agent_avatar_change" if change else "agent_avatar_reset",
        outcome=outcome,
        reason=reason,
    )


async def _may_edit(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    change: bool,
) -> bool:
    if is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
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
    )
    return False


async def upload_agent_avatar(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    attachment: discord.Attachment,
) -> tuple[str, AvatarRow | None]:
    """Authorize, read only the supplied Discord attachment, and replace the avatar."""
    if not await _may_edit(
        interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=True
    ):
        return "Only a server admin can change a live agent's avatar.", None
    if attachment.size > MAX_UPLOAD_BYTES or not (attachment.content_type or "").startswith(
        "image/"
    ):
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=interaction.user.id,
            change=True,
            outcome="error",
            reason="invalid_image",
        )
        return "Attach one PNG, JPG, GIF, or WebP image of at most 2 MB.", None
    try:
        async with asyncio.timeout(15):
            body = await attachment.read()
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError("attachment exceeds 2 MB")
        png = await asyncio.to_thread(normalize_avatar_image, body)
    except (discord.HTTPException, TimeoutError, ValueError):
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=interaction.user.id,
            change=True,
            outcome="error",
            reason="invalid_image",
        )
        return "I could not use that image. Attach one PNG, JPG, GIF, or WebP under 2 MB.", None
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session,
            platform="discord",
            external_id=str(interaction.user.id),
            tenant_id=tenant_id,
        )
        avatar = await replace_avatar(
            session,
            tenant_id=tenant_id,
            agent_name=agent_name,
            png=png,
            source="upload",
            updated_by_account_id=actor.account_id,
        )
    await _audit(
        runtime,
        tenant_id=tenant_id,
        user_id=interaction.user.id,
        change=True,
        outcome="allowed",
        reason="completed",
    )
    return "Avatar changed. Platform caches can keep the previous image.", avatar


async def reset_agent_avatar(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> tuple[str, AvatarRow | None]:
    """Restore the generated avatar with a fresh public token."""
    if not await _may_edit(
        interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=False
    ):
        return "Only a server admin can reset a live agent's avatar.", None
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
        )
    await _audit(
        runtime,
        tenant_id=tenant_id,
        user_id=interaction.user.id,
        change=False,
        outcome="allowed",
        reason="completed",
    )
    return "Generated avatar restored. Platform caches can keep the previous image.", avatar
