"""Admin-only avatar writes from the Discord setup panel and slash command."""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlsplit

import aiohttp
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_avatar_image import MAX_UPLOAD_BYTES, normalize_avatar_image
from daimon.core.agent_identity import is_builtin_agent
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.stores.agent_avatars import AvatarRow, replace_avatar, reset_avatar
from daimon.core.stores.identity import get_or_create_platform_principal

import discord


def _trusted_attachment_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"cdn.discordapp.com", "media.discordapp.net"}
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


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
    if runtime.settings.agent_identity.enabled and is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]
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


async def upload_agent_avatar(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    attachment: discord.Attachment,
) -> tuple[str, AvatarRow | None]:
    """Authorize, read only the supplied Discord attachment, and replace the avatar."""
    if not runtime.settings.agent_identity.enabled:
        await _may_edit(
            interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=True
        )
        return "Agent pictures are turned off.", None
    if not await _may_edit(
        interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=True
    ):
        return "Only admins can change this agent's picture. Ask an admin to change it.", None
    if attachment.size > MAX_UPLOAD_BYTES:
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=interaction.user.id,
            change=True,
            outcome="error",
            reason="invalid_image",
            agent_name=agent_name,
        )
        return "That picture is too big. Choose one up to 2 MB.", None
    if not (attachment.content_type or "").startswith("image/") or not _trusted_attachment_url(
        attachment.url
    ):
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=interaction.user.id,
            change=True,
            outcome="error",
            reason="invalid_image",
            agent_name=agent_name,
        )
        return "We couldn't use that picture. Use PNG, JPG, GIF or WebP.", None
    try:
        async with asyncio.timeout(15):
            body = await attachment.read()
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError("attachment exceeds 2 MB")
        png = await asyncio.to_thread(normalize_avatar_image, body)
    except (aiohttp.ClientError, discord.HTTPException, TimeoutError):
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=interaction.user.id,
            change=True,
            outcome="error",
            reason="invalid_image",
            agent_name=agent_name,
        )
        return "We couldn't read that file. Upload it again.", None
    except ValueError as exc:
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=interaction.user.id,
            change=True,
            outcome="error",
            reason="invalid_image",
            agent_name=agent_name,
        )
        if "2 MB" in str(exc):
            return "That picture is too big. Choose one up to 2 MB.", None
        return "We couldn't use that picture. Use PNG, JPG, GIF or WebP.", None
    if not await _may_edit(
        interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=True
    ):
        return "This agent is no longer available.", None
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
        agent_name=agent_name,
    )
    return "Picture changed.", avatar


async def reset_agent_avatar(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> tuple[str, AvatarRow | None]:
    """Restore the generated avatar with a fresh public token."""
    if not runtime.settings.agent_identity.enabled:
        await _may_edit(
            interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=False
        )
        return "Agent pictures are turned off.", None
    if not await _may_edit(
        interaction, runtime, tenant_id=tenant_id, agent_name=agent_name, change=False
    ):
        return "Only admins can change this agent's picture. Ask an admin to change it.", None
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
        agent_name=agent_name,
    )
    return "Default picture restored.", avatar
