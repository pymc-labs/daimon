"""CascadePreviewView + build_cascade_preview_container — red accent, confirm/cancel.

Confirm → opens DeleteConfirmModal. Cancel → re-renders the main panel.
"""

from __future__ import annotations

import uuid

from daimon.adapters.discord import layout, theme
from daimon.adapters.discord.privacy_panel.modal import DeleteConfirmModal
from daimon.adapters.discord.privacy_panel.state import PurgePreview
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.privacy import DELETE_PAUSED, delete_scope_lines

import discord


def build_cascade_preview_container(
    preview: PurgePreview, *, bot_display_name: str = "daimon"
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Red-accent V2 container for the cascade delete preview.

    Opens on what Delete removes and leaves (`delete_scope_lines`).

    Pure: no I/O, no ActionRows. The view shell appends hairline + ActionRow.
    """
    will_happen_rows: list[str] = []

    def amount(count: int, singular: str, plural: str | None = None) -> str:
        return f"**{count}** {singular if count == 1 else plural or singular + 's'}"

    if preview.linked_principals.count > 0:
        ex = preview.linked_principals.example or "—"
        n = amount(preview.linked_principals.count, "linked account")
        will_happen_rows.append(f"-# 🔑 Remove {n}, for example `{ex}`")
    if preview.routines.count > 0:
        will_happen_rows.append(f"-# ⏰ Cancel {amount(preview.routines.count, 'routine')}")
    if preview.principal_links.count > 0:
        will_happen_rows.append(
            f"-# 🔗 Remove {amount(preview.principal_links.count, 'link')} between your accounts"
        )
    if preview.user_configs.count > 0:
        will_happen_rows.append("-# ⚙ Remove your saved settings")
    if preview.user_skills.count > 0:
        ex = preview.user_skills.example or "—"
        n = amount(preview.user_skills.count, "skill")
        will_happen_rows.append(f"-# 🧰 Forget {n} you synced, for example `{ex}`")
    if preview.github_credentials.count > 0:
        ex = preview.github_credentials.example or "—"
        n = preview.github_credentials.count
        will_happen_rows.append(
            f"-# 🔑 Delete {amount(n, 'saved GitHub key')}, for example the one for `{ex}`"
        )
    if preview.github_user_links.count > 0:
        will_happen_rows.append(
            f"-# 🔗 Unlink {amount(preview.github_user_links.count, 'GitHub account')}"
        )
    if preview.github_oauth_states.count > 0:
        will_happen_rows.append(
            f"-# 🤝 Delete {amount(preview.github_oauth_states.count, 'GitHub sign-in record')}"
        )
    if preview.mcp_tokens.count > 0:
        will_happen_rows.append(
            f"-# 🎫 Delete {amount(preview.mcp_tokens.count, 'Daimon access token')}"
        )
    if preview.agent_github_binding.count > 0:
        n = preview.agent_github_binding.count
        will_happen_rows.append(f"-# 🤖 Remove {amount(n, 'link')} from agents to your GitHub keys")
    if preview.slack_user_tokens.count > 0:
        will_happen_rows.append(
            f"-# 🔐 Delete {amount(preview.slack_user_tokens.count, 'saved Slack access token')}"
        )
    if preview.slack_turn_contexts.count > 0:
        n = amount(preview.slack_turn_contexts.count, "temporary Slack request record")
        will_happen_rows.append(f"-# 💬 Delete {n}")
    if preview.direct_message_conversations.count > 0:
        n = amount(preview.direct_message_conversations.count, "private conversation")
        will_happen_rows.append(f"-# 💬 Delete Daimon's saved data for {n}")
    if preview.channel_admins.count > 0:
        will_happen_rows.append(
            f"-# Remove you from {amount(preview.channel_admins.count, 'channel admin list')}"
        )
    if preview.account.count > 0:
        will_happen_rows.append("-# 🪪 Remove your account")

    body_rows: list[str] = [
        "⚡ **What will happen**",
        "",
        *(will_happen_rows if will_happen_rows else ["-# _Nothing to delete._"]),
        "",
        "📋 **What is intentionally kept elsewhere**",
        "",
        "-# Usage records are retained for service integrity and cannot be erased on request.",
        "-# Uploaded skill files stay in Managed Agents; guild agents may keep using them.",
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
        discord.ui.TextDisplay("\n\n".join(delete_scope_lines(bot_display_name))),
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
    ) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.account_id = account_id
        self.allowed_user_id = allowed_user_id
        self.user_name = user_name
        self.preview = preview

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
        container = build_cascade_preview_container(preview, bot_display_name=bot_display_name)
        container.add_item(layout.hairline())
        container.add_item(action_row)
        self.add_item(container)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not self.runtime.settings.privacy.delete_enabled:
            await interaction.response.send_message(DELETE_PAUSED, ephemeral=True)
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
        )
        await interaction.response.edit_message(
            view=new_view,
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
