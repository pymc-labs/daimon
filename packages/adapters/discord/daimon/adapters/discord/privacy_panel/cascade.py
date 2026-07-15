"""CascadePreviewView + build_cascade_preview_container — red accent, confirm/cancel.

Confirm → opens DeleteConfirmModal. Cancel → re-renders the main panel.
"""

from __future__ import annotations

import uuid

from daimon.adapters.discord import layout, theme
from daimon.adapters.discord.privacy_panel.modal import DeleteConfirmModal
from daimon.adapters.discord.privacy_panel.state import PurgePreview
from daimon.adapters.discord.runtime import DiscordRuntime

import discord


def build_cascade_preview_container(
    preview: PurgePreview,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Red-accent V2 container for the cascade delete preview.

    Pure: no I/O, no ActionRows. The view shell appends hairline + ActionRow.
    """
    will_happen_rows: list[str] = []
    if preview.linked_principals.count > 0:
        ex = preview.linked_principals.example or "—"
        will_happen_rows.append(
            f"-# 🔑 Remove **{preview.linked_principals.count}** linked principal(s) (e.g. `{ex}`)"
        )
    if preview.routines.count > 0:
        ex = preview.routines.example or "—"
        will_happen_rows.append(
            f"-# ⏰ Cancel **{preview.routines.count}** scheduled routine(s) (e.g. `{ex}`)"
        )
    if preview.principal_links.count > 0:
        will_happen_rows.append(
            f"-# 🔗 Remove **{preview.principal_links.count}** principal link(s)"
        )
    if preview.user_configs.count > 0:
        will_happen_rows.append(f"-# ⚙ Remove **{preview.user_configs.count}** user config row(s)")
    if preview.user_skills.count > 0:
        ex = preview.user_skills.example or "—"
        will_happen_rows.append(
            f"-# 🧰 Remove **{preview.user_skills.count}** synced skill ledger row(s) (e.g. `{ex}`)"
        )
    if preview.github_credentials.count > 0:
        ex = preview.github_credentials.example or "—"
        n = preview.github_credentials.count
        will_happen_rows.append(f"-# 🔑 Delete **{n}** stored GitHub credential(s) (`{ex}`)")
    if preview.github_oauth_states.count > 0:
        will_happen_rows.append(
            f"-# 🤝 Remove **{preview.github_oauth_states.count}** GitHub OAuth handshake record(s)"
        )
    if preview.mcp_tokens.count > 0:
        will_happen_rows.append(
            f"-# 🎫 Revoke **{preview.mcp_tokens.count}** per-agent MCP token(s)"
        )
    if preview.agent_github_binding.count > 0:
        n = preview.agent_github_binding.count
        will_happen_rows.append(f"-# 🤖 Remove **{n}** per-agent GitHub credential link(s)")
    if preview.account.count > 0:
        will_happen_rows.append("-# 🪪 Remove the account row itself")

    body_rows: list[str] = [
        "⚡ **What will happen**",
        *(will_happen_rows if will_happen_rows else ["-# _(nothing to delete)_"]),
        "",
        "🔐 **What stays in Managed Agents**",
        "-# Agent definitions, system prompts, MCP credentials",
        "-# Session transcripts, turn message content",
        "-# Skill repo references — the repos themselves stay on GitHub",
        "-# Retention is governed by Anthropic's Managed Agents policy.",
        "",
        "📋 **What is intentionally kept elsewhere**",
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
        container = build_cascade_preview_container(preview)
        container.add_item(layout.hairline())
        container.add_item(action_row)
        self.add_item(container)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
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
            preview=self.preview,
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
