"""`/billing` slash command — (user, guild)-attributed visibility panel.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.
"""

from datetime import UTC, datetime
from typing import cast

import anthropic
import structlog
from daimon.adapters.discord.billing_panel.panel import BillingPanelView
from daimon.adapters.discord.billing_panel.read import (
    is_guild_admin,
    load_billing_snapshot,
)
from daimon.adapters.discord.checks import require_registered_guild
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import get_or_create_platform_principal

import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = structlog.get_logger()
BotInteraction = Interaction[commands.Bot]


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


@app_commands.guild_only()
class BillingCog(commands.Cog):
    """Read-only (user, guild)-attributed billing panel."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="billing",
        description="Show your billing usage (admins see per-member breakdown)",
    )
    @require_registered_guild
    async def billing(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rid = generate_request_id()
        try:
            runtime = _get_runtime(interaction)
            assert interaction.guild_id is not None, "guild_only ensures guild context"
            assert interaction.guild is not None, "guild_only ensures guild context"
            now = datetime.now(UTC)
            since = datetime(now.year, now.month, 1, tzinfo=UTC)
            is_admin = is_guild_admin(interaction)
            async with runtime.sessionmaker() as session:
                state = await load_billing_snapshot(
                    session,
                    guild=interaction.guild,
                    guild_id=str(interaction.guild_id),
                    caller_user_id=str(interaction.user.id),
                    is_admin=is_admin,
                    since=since,
                )
                # Tenant ids are derived deterministically from (platform, guild) —
                # the same uuid the turn pipeline bills against.
                tenant_id = derive_tenant_uuid(
                    platform="discord", workspace_id=str(interaction.guild_id)
                )
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_id=str(interaction.user.id),
                )
                account_id = principal.account_id
            view = BillingPanelView(
                state,
                runtime=runtime,
                allowed_user_id=interaction.user.id,
                is_admin=is_admin,
                account_id=account_id,
                now=now,
                since=since,
            )
            await interaction.followup.send(
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException) as exc:
            log.warning("billing.handler.failed", rid=rid, error=str(exc))
            await interaction.followup.send(
                render_error(exc, request_id=rid),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BillingCog(bot))
