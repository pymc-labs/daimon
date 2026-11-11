"""`/settings`: the shared Settings panel (`daimon.core.settings_panel`) for this channel or
thread. One message: a select per control and a Back button, redrawn on each press. Every
change is applied by the core writer, which re-checks the presser live.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.
"""

import uuid
from collections.abc import Callable, Coroutine
from typing import Any, cast

from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.theme import COLOR_BLURPLE, COLOR_RED
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.features.service import FeatureActor
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.settings_panel import PanelScreen, chat_actor, open_panel, press, tenant_agents

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

RUN_IN_CHANNEL = "Run /settings in a server channel."
OTHER_SERVER = "This control belongs to another server. Run /settings here instead."


async def _actor(
    runtime: DiscordRuntime, interaction: BotInteraction, tenant_id: uuid.UUID
) -> FeatureActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return await is_guild_admin(interaction, action=action)

    return await chat_actor(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        user_id=str(interaction.user.id),
        admin_for=admin_for,
    )


def place_of(interaction: BotInteraction, tenant_id: uuid.UUID) -> FeaturePlace:
    """The channel the command was run in and, inside a thread, the thread."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return FeaturePlace.of(tenant_id, "discord", str(channel.parent_id), str(channel.id))
    if channel is None or interaction.guild is None:
        raise UserFacingError(RUN_IN_CHANNEL)
    return FeaturePlace.of(tenant_id, "discord", str(channel.id), None)


class SettingsView(discord.ui.View):
    """One screen of the panel: its selects (each one control) and a way back."""

    def __init__(
        self,
        runtime: DiscordRuntime,
        place: FeaturePlace,
        screen: PanelScreen,
    ) -> None:
        super().__init__(timeout=300)
        self.runtime, self.place = runtime, place
        for control in screen.selects:
            select: discord.ui.Select[SettingsView] = discord.ui.Select(
                placeholder=control.placeholder,
                options=[
                    discord.SelectOption(
                        label=o.label[:100], value=o.value, description=o.description[:100] or None
                    )
                    for o in control.options
                ],
            )
            select.callback = self.change
            self.add_item(select)
        if screen.back is not None:
            back: discord.ui.Button[SettingsView] = discord.ui.Button(label=screen.back.label)
            back.callback = self._go(screen.back.value)  # type: ignore[method-assign]
            self.add_item(back)

    def _go(self, op: str) -> Callable[[BotInteraction], Coroutine[Any, Any, None]]:
        async def go(interaction: BotInteraction) -> None:
            await self.pressed(interaction, op)

        return go

    async def change(self, interaction: BotInteraction) -> None:
        values = cast("list[object]", (interaction.data or {}).get("values") or [None])
        await self.pressed(interaction, values[0])

    async def pressed(self, interaction: BotInteraction, raw: object) -> None:
        await interaction.response.defer()
        if (
            await resolve_tenant_for_interaction(interaction.client, interaction)
            != self.place.tenant_id
        ):
            await interaction.followup.send(embed=build_notice_embed(OTHER_SERVER), ephemeral=True)
            return
        screen = await press(
            self.runtime.sessionmaker,
            self.runtime.settings,
            await _actor(self.runtime, interaction, self.place.tenant_id),
            raw,
            self.place,
            agents=tenant_agents(
                self.runtime.anthropic, self.place.tenant_id, records=self.runtime.sessionmaker
            ),
        )
        if screen is not None:
            await _draw(interaction, self.runtime, self.place, screen)


def screen_embed(screen: PanelScreen) -> discord.Embed:
    text = "\n".join(screen.lines)
    if screen.notice:
        text = f"{text}\n\n{screen.notice}"
    color = COLOR_BLURPLE if screen.ok else COLOR_RED
    return discord.Embed(title=screen.title, description=text[:4096], color=color)


async def _draw(
    interaction: BotInteraction,
    runtime: DiscordRuntime,
    place: FeaturePlace,
    screen: PanelScreen,
) -> None:
    view = SettingsView(runtime, place, screen)
    await interaction.edit_original_response(
        embed=screen_embed(screen),
        view=view if view.children else None,
        allowed_mentions=discord.AllowedMentions.none(),
    )


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
            place = place_of(interaction, tenant_id)
            actor = await _actor(runtime, interaction, tenant_id)
            screen = await open_panel(
                runtime.sessionmaker,
                runtime.settings,
                actor,
                place,
                agents=tenant_agents(runtime.anthropic, tenant_id, records=runtime.sessionmaker),
            )
            await _draw(interaction, runtime, place, screen)
        except (DaimonError, discord.HTTPException) as exc:
            await interaction.edit_original_response(
                content=None,
                embed=build_notice_embed(render_error(exc, request_id=generate_request_id())),
                allowed_mentions=discord.AllowedMentions.none(),
            )
