"""Classic Discord embed panels for GitHub setup screens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

from daimon.adapters.discord.agent_setup.expiry import ExpiringView
from daimon.adapters.discord.agent_setup.navigation import (
    INVOKER_ONLY_MESSAGE,
    PANEL_TIMEOUT_SECONDS,
    STALE_PANEL_MESSAGE,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.theme import COLOR_AMBER, COLOR_BLURPLE, COLOR_GREEN, COLOR_RED

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.agent_setup.navigation import PanelViewBase


class EmbedActionRow(discord.ui.ActionRow[discord.ui.LayoutView]):
    """Construction row for controls later flattened into a classic embed view."""

    def __init__(self, *children: discord.ui.Item[Any]) -> None:
        super().__init__()
        for child in children:
            self.add_item(child)

    def add_item(self, item: discord.ui.Item[Any]) -> Self:
        return super().add_item(cast("discord.ui.Item[discord.ui.LayoutView]", item))


def _panel_embed(texts: list[str]) -> discord.Embed:
    first = texts[0] if texts else "GitHub"
    if first.startswith("## "):
        title, _, description = first[3:].partition("\n")
    elif "?" in first and first.index("?") < 150:
        end = first.index("?") + 1
        title, description = first[:end], first[end:].strip()
    else:
        title, description = "GitHub", first
    lead = first.casefold()
    color = (
        COLOR_RED
        if any(word in lead for word in ("disconnect", "unlink", "unsaved work"))
        else COLOR_AMBER
        if "waiting" in lead
        else COLOR_GREEN
        if "connected" in lead
        else COLOR_BLURPLE
    )
    embed = discord.Embed(title=title[:256], description=description[:4096], color=color)
    for item in texts[1:]:
        name, _, value = item.partition("\n")
        if len(name) > 80 or not value:
            name, value = "Details", item
        embed.add_field(name=name[:256], value=value[:1024], inline=False)
    return embed


class GitHubEmbedPanel(ExpiringView, discord.ui.View):
    """Flatten existing GitHub controls into a classic view and render text as an embed."""

    def __init__(self, state: PanelState, *, runtime: DiscordRuntime, allowed_user_id: int) -> None:
        super().__init__(timeout=PANEL_TIMEOUT_SECONDS)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id
        self.embed = _panel_embed([])
        self._render_message: discord.Message | None = None

    def attach_message(self, message: discord.Message) -> None:
        """Keep the actual private panel message for expiry after a follow-up send."""
        self._render_message = message

    def add_item(self, item: discord.ui.Item[Any]) -> Self:
        if isinstance(item, discord.ui.Container):
            texts: list[str] = []
            row = 0
            for child in item.children:
                if isinstance(child, discord.ui.TextDisplay):
                    texts.append(child.content)
                elif isinstance(child, discord.ui.ActionRow):
                    for control in child.children:
                        control.row = row
                        super().add_item(control)
                    row += 1
            self.embed = _panel_embed(texts)
            return self
        return super().add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(INVOKER_ONLY_MESSAGE, ephemeral=True)
            return False
        if self._is_superseded():
            await interaction.response.send_message(STALE_PANEL_MESSAGE, ephemeral=True)
            return False
        return True

    async def swap_to(
        self, interaction: discord.Interaction, view: GitHubEmbedPanel | PanelViewBase
    ) -> None:
        if self._is_superseded():
            await interaction.response.send_message(STALE_PANEL_MESSAGE, ephemeral=True)
            return
        target = view.bind_render_interaction(interaction, panel=self.state)
        if isinstance(view, GitHubEmbedPanel):
            if interaction.response.is_done():
                message = await interaction.edit_original_response(
                    content=None,
                    embed=view.embed,
                    view=target,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                view.attach_message(message)
            else:
                await interaction.response.edit_message(
                    content=None,
                    embed=view.embed,
                    view=target,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                view.attach_message(await interaction.original_response())
        elif interaction.response.is_done():
            await interaction.edit_original_response(
                content=None,
                embed=None,
                view=target,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.edit_message(
                content=None,
                embed=None,
                view=target,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_timeout(self) -> None:
        if self._is_superseded():
            return
        if self._render_panel is not None:
            self._render_panel.render_seq += 1
        try:
            expired = discord.Embed(
                title="GitHub panel expired",
                description="Run `/github home` to continue.",
                color=COLOR_BLURPLE,
            )
            if self._render_message is not None:
                await self._render_message.edit(
                    content=None,
                    embed=expired,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            elif self._render_interaction is not None:
                await self._render_interaction.edit_original_response(
                    content=None,
                    embed=expired,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.NotFound:
            return
