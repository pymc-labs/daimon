"""`/settings`: the shared Settings panel (`daimon.core.scope_settings`) for this channel or
thread, one select per row. Every press is applied by the core writer, which re-checks
the presser live.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.
"""

import uuid
from typing import cast

from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.theme import COLOR_BLURPLE
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.scope_settings import (
    SettingsActor,
    SettingsPanel,
    SettingsPlace,
    apply_choice,
    chat_actor,
    load_panel,
)

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

RUN_IN_CHANNEL = "Run /settings in a server channel."
OTHER_SERVER = "This control belongs to another server. Run /settings here instead."


async def _actor(
    runtime: DiscordRuntime, interaction: BotInteraction, tenant_id: uuid.UUID
) -> SettingsActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return await is_guild_admin(interaction, action=action)

    return await chat_actor(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        user_id=str(interaction.user.id),
        admin_for=admin_for,
    )


def place_of(interaction: BotInteraction) -> SettingsPlace:
    """The channel the command was run in and, inside a thread, the thread."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return SettingsPlace.of("discord", str(channel.parent_id), str(channel.id))
    if channel is None or interaction.guild is None:
        raise UserFacingError(RUN_IN_CHANNEL)
    return SettingsPlace.of("discord", str(channel.id), None)


class SettingsView(discord.ui.View):
    def __init__(
        self,
        runtime: DiscordRuntime,
        tenant_id: uuid.UUID,
        place: SettingsPlace,
        panel: SettingsPanel,
    ) -> None:
        super().__init__(timeout=300)
        self.runtime, self.tenant_id, self.place = runtime, tenant_id, place
        for row in panel.rows:
            if row.choices:
                select: discord.ui.Select[SettingsView] = discord.ui.Select(
                    placeholder=row.placeholder,
                    options=[
                        discord.SelectOption(label=text, value=v) for v, text in row.choices[:25]
                    ],
                )
                select.callback = self.change
                self.add_item(select)

    async def change(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True)
        if await resolve_tenant_for_interaction(interaction.client, interaction) != self.tenant_id:
            await interaction.followup.send(embed=build_notice_embed(OTHER_SERVER), ephemeral=True)
            return
        values = cast("list[object]", (interaction.data or {}).get("values") or [None])
        done = await apply_choice(
            self.runtime.sessionmaker,
            self.runtime.settings,
            await _actor(self.runtime, interaction, self.tenant_id),
            values[0],
            self.place,
        )
        if done is not None:
            embed = build_notice_embed(done[1], neutral=done[0])
            await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.guild_only()
class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="settings", description=DESCRIPTIONS["settings"])
    @require_registered_guild
    async def settings(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            if tenant_id is None:
                raise UserFacingError(RUN_IN_CHANNEL)
            place = place_of(interaction)
            actor = await _actor(runtime, interaction, tenant_id)
            panel = await load_panel(runtime.sessionmaker, runtime.settings, actor, place)
            embed = discord.Embed(title=panel.title, description=panel.subline, color=COLOR_BLURPLE)
            for row in panel.rows:
                embed.add_field(name=row.name, value=row.summary[:1024], inline=False)
            view = SettingsView(runtime, tenant_id, place, panel)
            await interaction.edit_original_response(
                embed=embed,
                view=view if view.children else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, discord.HTTPException) as exc:
            await interaction.edit_original_response(
                content=None,
                embed=build_notice_embed(render_error(exc, request_id=generate_request_id())),
                allowed_mentions=discord.AllowedMentions.none(),
            )
