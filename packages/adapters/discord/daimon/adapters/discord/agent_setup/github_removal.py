"""Private Discord notice after GitHub removes an installation."""

from __future__ import annotations

import structlog
from daimon.adapters.discord.agent_setup.github_card_ui import github_embed
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_panel import connect_link, sync_connect_admin
from daimon.core.stores.github_access_requests import list_server_admin_recipients
from daimon.core.stores.github_removal_notices import RemovalNotice
from daimon.core.stores.tenants import get_tenant

import discord

_log = structlog.get_logger(__name__)


async def send_removal_dm(
    bot: discord.Client, runtime: DiscordRuntime, notice: RemovalNotice
) -> bool:
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, notice.tenant_id)
        recipients = await list_server_admin_recipients(
            session, tenant_id=notice.tenant_id, platform="discord", limit=50
        )
    if tenant is None:
        return False
    guild = bot.get_guild(int(tenant.external_id))
    if guild is None:
        return False
    landed = False
    for recipient in recipients:
        try:
            member = guild.get_member(int(recipient.platform_user_id))
            if member is None:
                member = await guild.fetch_member(int(recipient.platform_user_id))
            if not is_member_guild_admin(member, guild_owner_id=guild.owner_id):
                continue
            async with runtime.sessionmaker.begin() as session:
                await sync_connect_admin(
                    session,
                    tenant_id=notice.tenant_id,
                    platform="discord",
                    platform_user_id=recipient.platform_user_id,
                    verified_tenant_admin=True,
                )
                url = await connect_link(
                    session,
                    settings=runtime.settings,
                    tenant_id=notice.tenant_id,
                    platform="discord",
                    platform_user_id=recipient.platform_user_id,
                    verified_tenant_admin=True,
                    workspace_label=guild.name,
                    requester_label=member.display_name,
                )
            from daimon.adapters.discord.agent_setup.github_home import connect_button_view

            view = connect_button_view(url)
            await member.send(
                embed=github_embed(
                    f"Daimon was removed from **{notice.account_login}** on GitHub.",
                    state="danger",
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            landed = True
        except Exception:
            _log.exception("github_removal.admin_dm_failed", tenant_id=str(notice.tenant_id))
            continue
    return landed
