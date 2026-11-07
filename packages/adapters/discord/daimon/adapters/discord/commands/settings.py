"""`/settings`: this place's approvals, thread participation, progress plans and cost footer.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.

The panel is the shared model (`daimon.core.settings_card`): one select per row with
the levels and values the caller may change. A choice is applied by
`daimon.core.scope_settings.change_setting`, which re-checks who may make it on every
press; approvals need a native server admin, checked live.
"""

import uuid
from typing import cast

from daimon.adapters.discord.checks import (
    is_guild_admin,
    is_platform_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.theme import COLOR_BLURPLE
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import DaimonError, StaleChannelValue, UserFacingError
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.scope_settings import (
    SettingsActor,
    SettingsDefaults,
    SettingsPlace,
    actor_account,
    change_setting,
    read_settings,
)
from daimon.core.settings_card import (
    SettingsPanel,
    build_settings_panel,
    decode_choice,
    may_change_for,
)

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

RUN_IN_CHANNEL = "Run /settings in a server channel."
OTHER_SERVER = "This control belongs to another server. Run /settings here instead."


def build_settings_embed(panel: SettingsPanel) -> discord.Embed:
    embed = discord.Embed(title=panel.title, description=panel.subline, color=COLOR_BLURPLE)
    for row in panel.rows:
        embed.add_field(name=row.name, value=row.summary[:1024], inline=False)
    return embed


def place_of(interaction: BotInteraction) -> SettingsPlace:
    """The channel the command was run in and, inside a thread, the thread."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return SettingsPlace.of("discord", str(channel.parent_id), str(channel.id))
    if channel is None or interaction.guild is None:
        raise UserFacingError(RUN_IN_CHANNEL)
    return SettingsPlace.of("discord", str(channel.id), None)


def settings_actor(
    interaction: BotInteraction, tenant_id: uuid.UUID, account_id: uuid.UUID | None
) -> SettingsActor:
    """The presser, checked live on Discord for each delegated action."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return await is_guild_admin(interaction, action=action)

    return SettingsActor(
        tenant_id=tenant_id,
        account_id=account_id,
        surface="panel",
        native_user_id=str(interaction.user.id),
        admin_for=admin_for,
    )


class SettingsView(discord.ui.View):
    """One select per row; authority is re-read on every press."""

    def __init__(
        self,
        runtime: DiscordRuntime,
        tenant_id: uuid.UUID,
        place: SettingsPlace,
        panel: SettingsPanel,
    ) -> None:
        super().__init__(timeout=300)
        self.runtime = runtime
        self.tenant_id = tenant_id
        self.place = place
        for row in panel.rows:
            if not row.choices:
                continue
            select: discord.ui.Select[SettingsView] = discord.ui.Select(
                placeholder=row.placeholder,
                options=[
                    discord.SelectOption(label=choice.label, value=choice.value)
                    for choice in row.choices[:25]
                ],
            )
            select.callback = self.change
            self.add_item(select)

    async def change(self, interaction: BotInteraction) -> None:
        await interaction.response.defer(ephemeral=True)
        tenant = await resolve_tenant_for_interaction(interaction.client, interaction)
        if tenant != self.tenant_id:
            await interaction.followup.send(embed=build_notice_embed(OTHER_SERVER), ephemeral=True)
            return
        data = interaction.data or {}
        values = data.get("values", [])
        raw: object = (
            cast("list[object]", values)[0] if isinstance(values, list) and values else None
        )
        choice = decode_choice(raw)
        if choice is None:
            return
        key, level, value = choice
        try:
            account_id = await actor_account(
                self.runtime.sessionmaker,
                tenant_id=self.tenant_id,
                platform="discord",
                user_id=str(interaction.user.id),
            )
            change = await change_setting(
                self.runtime.sessionmaker,
                self.runtime.settings,
                settings_actor(interaction, self.tenant_id, account_id),
                key=key,
                level=level,
                value=value,
                place=self.place,
            )
        except (ChannelChangeRefused, StaleChannelValue) as exc:
            await interaction.followup.send(embed=build_notice_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=build_notice_embed(change.note, neutral=True), ephemeral=True
        )


async def load_settings_panel(
    runtime: DiscordRuntime,
    interaction: BotInteraction,
    tenant_id: uuid.UUID,
    place: SettingsPlace,
) -> SettingsPanel:
    views = await read_settings(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        place=place,
        defaults=SettingsDefaults.from_settings(runtime.settings, "discord"),
    )
    return build_settings_panel(
        views,
        place=place,
        may_change=may_change_for(
            admin=await is_guild_admin(interaction), native_admin=is_platform_admin(interaction)
        ),
    )


@app_commands.guild_only()
class SettingsCog(commands.Cog):
    """The Settings group for the channel or thread the command is run in."""

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
            panel = await load_settings_panel(runtime, interaction, tenant_id, place)
            view = SettingsView(runtime, tenant_id, place, panel)
            await interaction.edit_original_response(
                embed=build_settings_embed(panel),
                view=view if view.children else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, discord.HTTPException) as exc:
            await interaction.edit_original_response(
                content=None,
                embed=build_notice_embed(render_error(exc, request_id=generate_request_id())),
                allowed_mentions=discord.AllowedMentions.none(),
            )
