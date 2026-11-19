"""CascadePreviewView + build_cascade_preview_container — red accent, confirm/cancel.

Confirm → opens DeleteConfirmModal. Cancel → re-renders the main panel.
"""

from __future__ import annotations

import uuid

from daimon.adapters.discord import layout, theme
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.privacy_panel.modal import DeleteConfirmModal
from daimon.adapters.discord.privacy_panel.state import PurgePreview
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.privacy import DELETE_PAUSED, cascade_lines, delete_scope_lines

import discord


def build_cascade_preview_container(
    preview: PurgePreview, *, bot_display_name: str = "daimon", providers: tuple[str, ...] = ()
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Red-accent V2 container for the cascade delete preview.

    Opens on what Delete removes and leaves (`delete_scope_lines`).

    Pure: no I/O, no ActionRows. The view shell appends hairline + ActionRow.
    """
    will_happen_rows = [f"-# {line}" for line in cascade_lines(preview, "discord")]

    def amount(count: int, singular: str) -> str:
        return f"**{count}** {singular if count == 1 else singular + 's'}"

    shared_conversations = preview.retained_shared_conversations.count
    shared_rows = (
        [
            f"-# Your messages in {amount(shared_conversations, 'shared conversation')}"
            " stay for the people still using them."
        ]
        if shared_conversations > 0
        else []
    )

    body_rows: list[str] = [
        "⚡ **What will happen**",
        "",
        *(will_happen_rows if will_happen_rows else ["-# _Nothing to delete._"]),
        "",
        "📋 **What is intentionally kept elsewhere**",
        "",
        "-# Usage records are retained for service integrity and cannot be erased on request.",
        *shared_rows,
        "-# Uploaded skill files stay in Managed Agents; shared agents may keep using them.",
        "-# The GitHub-side OAuth authorization stays on your GitHub account"
        " — revoke it at github.com/settings/applications.",
        "",
        "-# you'll type your username to confirm on the next step",
    ]
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header(
            "🗑 Confirm delete",
            subtext="**irreversible** — re-onboarding starts from scratch",
        ),
        discord.ui.TextDisplay("\n\n".join(delete_scope_lines(bot_display_name, providers))),
        layout.hairline(),
        discord.ui.TextDisplay("\n".join(body_rows)),
        accent_colour=theme.COLOR_RED,
    )
    return container


class CascadePreviewView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        account_id: uuid.UUID,
        allowed_user_id: int,
        user_name: str,
        preview: PurgePreview,
        monitored: bool = False,
        providers: tuple[str, ...] = (),
    ) -> None:
        super().__init__(timeout=600)
        self.providers = providers
        self.runtime = runtime
        self.account_id = account_id
        self.allowed_user_id = allowed_user_id
        self.user_name = user_name
        self.preview = preview
        self.monitored = monitored

        # Programmatic buttons — decorator pattern does not work on LayoutView subclasses
        confirm_btn: discord.ui.Button[CascadePreviewView] = discord.ui.Button(
            label="🗑 I understand — confirm",
            style=discord.ButtonStyle.danger,
        )
        confirm_btn.callback = self._on_confirm  # type: ignore[method-assign]

        cancel_btn: discord.ui.Button[CascadePreviewView] = discord.ui.Button(
            label="◀ Cancel",
            style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._on_cancel  # type: ignore[method-assign]

        action_row: discord.ui.ActionRow[CascadePreviewView] = discord.ui.ActionRow(
            confirm_btn, cancel_btn
        )
        bot_display_name = (
            runtime.settings.discord.bot_display_name
            if runtime.settings.discord is not None
            else "daimon"
        )
        container = build_cascade_preview_container(
            preview, bot_display_name=bot_display_name, providers=providers
        )
        container.add_item(layout.hairline())
        container.add_item(action_row)
        self.add_item(container)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not self.runtime.settings.privacy.delete_enabled:
            await interaction.response.send_message(
                embed=build_notice_embed(DELETE_PAUSED), ephemeral=True
            )
            return
        await interaction.response.send_modal(
            DeleteConfirmModal(
                runtime=self.runtime,
                account_id=self.account_id,
                user_name=self.user_name,
            )
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        # Lazy import to avoid circular: cascade.py <-> panel.py
        from daimon.adapters.discord.privacy_panel.panel import PrivacyPanelView

        new_view = PrivacyPanelView(
            runtime=self.runtime,
            account_id=self.account_id,
            allowed_user_id=self.allowed_user_id,
            user_name=self.user_name,
            monitored=self.monitored,
            providers=self.providers,
        )
        await interaction.response.edit_message(
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                embed=build_notice_embed("Only the command invoker can use these buttons."),
                ephemeral=True,
            )
            return False
        return True
