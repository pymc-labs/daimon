"""Admin-only shortcut for the current channel's reading rule."""

from __future__ import annotations

import uuid

from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.authz import build_subject
from daimon.core.channel_rules import set_channel_rule
from daimon.core.errors import DaimonError
from daimon.core.stores.identity import get_or_create_platform_principal

import discord


class MakeConfidentialView(discord.ui.View):
    def __init__(self, runtime: DiscordRuntime, tenant_id: uuid.UUID, channel_id: str) -> None:
        super().__init__(timeout=300)
        self.runtime = runtime
        self.tenant_id = tenant_id
        self.channel_id = channel_id

    @discord.ui.button(label="Make confidential", style=discord.ButtonStyle.secondary)
    async def make_confidential(
        self, interaction: discord.Interaction, button: discord.ui.Button[MakeConfidentialView]
    ) -> None:
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]
            return
        await interaction.response.defer(ephemeral=True)
        async with self.runtime.sessionmaker.begin() as session:
            actor = await get_or_create_platform_principal(
                session,
                tenant_id=self.tenant_id,
                platform="discord",
                external_id=str(interaction.user.id),
            )
        try:
            change = await set_channel_rule(
                self.runtime.anthropic,
                self.runtime.sessionmaker,
                tenant_id=self.tenant_id,
                platform="discord",
                channel_id=self.channel_id,
                readers="own",
                subject=build_subject(is_admin=True, platform_user_id=str(interaction.user.id)),
                default=self.runtime.deployment_default,
                actor_account_id=actor.account_id,
            )
        except DaimonError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.edit_original_response(
            content="\n".join(change.notes),
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
