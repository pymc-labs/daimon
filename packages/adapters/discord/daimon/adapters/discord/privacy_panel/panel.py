"""PrivacyPanelView + build_privacy_main_container + Policy/Export/Delete/Done buttons.

Main panel: no accent. Two lines on who stores what; the detail is the policy's.
Clicking Delete… transitions to the CascadePreviewView (red).
"""

from __future__ import annotations

import uuid

from daimon.adapters.discord import layout
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.privacy import PRIVACY_TITLE, privacy_lines

import discord


def _export_placeholder_message(bot_display_name: str) -> str:
    return (
        "📤 **Export** is not yet implemented.\n\n"
        f"When ready, this will produce a JSON dump of every {bot_display_name}-side row "
        "tied to your identity and either attach it here or DM you a 7-day "
        "signed URL."
    )


def build_privacy_main_container(
    *, bot_display_name: str = "daimon"
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Main panel V2 container — no accent: the title and who stores what.

    The detail categories live in the policy behind the Policy button. Pure:
    no I/O, no ActionRows. The view shell appends hairline + ActionRow.
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header(PRIVACY_TITLE),
        discord.ui.TextDisplay("\n\n".join(privacy_lines(bot_display_name))),
    )
    return container


class PrivacyPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        account_id: uuid.UUID,
        allowed_user_id: int,
        user_name: str,
    ) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.account_id = account_id
        self.allowed_user_id = allowed_user_id
        self.user_name = user_name
        self._bot_display_name = (
            runtime.settings.discord.bot_display_name
            if runtime.settings.discord is not None
            else "daimon"
        )

        # Programmatic buttons — decorator pattern does not work on LayoutView subclasses
        policy_btn: discord.ui.Button[PrivacyPanelView] = discord.ui.Button(
            label="📄 Policy",
            style=discord.ButtonStyle.link,
            url=str(runtime.settings.privacy_policy_url),
        )
        export_btn: discord.ui.Button[PrivacyPanelView] = discord.ui.Button(
            label="📤 Export",
            style=discord.ButtonStyle.secondary,
        )
        export_btn.callback = self._on_export  # type: ignore[method-assign]

        done_btn: discord.ui.Button[PrivacyPanelView] = discord.ui.Button(
            label="✓ Done",
            style=discord.ButtonStyle.secondary,
        )
        done_btn.callback = self._on_done  # type: ignore[method-assign]

        buttons = [policy_btn, export_btn]
        if runtime.settings.privacy.delete_enabled:
            delete_btn: discord.ui.Button[PrivacyPanelView] = discord.ui.Button(
                label="🗑 Delete…",
                style=discord.ButtonStyle.danger,
            )
            delete_btn.callback = self._on_delete  # type: ignore[method-assign]
            buttons.append(delete_btn)
        buttons.append(done_btn)
        action_row: discord.ui.ActionRow[PrivacyPanelView] = discord.ui.ActionRow(*buttons)
        container = build_privacy_main_container(bot_display_name=self._bot_display_name)
        container.add_item(layout.hairline())
        container.add_item(action_row)
        self.add_item(container)

    async def _on_export(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            _export_placeholder_message(self._bot_display_name),
            ephemeral=True,
        )

    async def _on_delete(self, interaction: discord.Interaction) -> None:
        if not self.runtime.settings.privacy.delete_enabled:
            await interaction.response.send_message(
                "Deleting your account is paused during the event.", ephemeral=True
            )
            return
        # Lazy import to avoid circular: panel.py <-> cascade.py
        from daimon.adapters.discord.privacy_panel.cascade import CascadePreviewView
        from daimon.adapters.discord.privacy_panel.read import load_purge_preview

        preview = await load_purge_preview(
            session_factory=self.runtime.sessionmaker,
            account_id=self.account_id,
        )
        cascade_view = CascadePreviewView(
            runtime=self.runtime,
            account_id=self.account_id,
            allowed_user_id=self.allowed_user_id,
            user_name=self.user_name,
            preview=preview,
        )
        await interaction.response.edit_message(
            view=cascade_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_done(self, interaction: discord.Interaction) -> None:
        # Controls-less re-render (view=None empties a V2 message).
        await interaction.response.edit_message(
            view=layout.static_view(
                build_privacy_main_container(bot_display_name=self._bot_display_name)
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.",
                ephemeral=True,
            )
            return False
        return True
