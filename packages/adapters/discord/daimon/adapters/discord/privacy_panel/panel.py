"""PrivacyPanelView + build_privacy_main_container + Policy/Export/Delete/Done buttons.

Main panel: no accent. Three trust-model groups in one TextDisplay.
Clicking Delete… transitions to the CascadePreviewView (red).
"""

from __future__ import annotations

import uuid

from daimon.adapters.discord import layout
from daimon.adapters.discord.privacy_panel.state import PurgePreview
from daimon.adapters.discord.runtime import DiscordRuntime

import discord


def _summary_line(preview: PurgePreview) -> str:
    """Render one-line summary of held-data counts for the header subtext."""
    parts: list[str] = []
    if preview.linked_principals.count > 0:
        parts.append(f"{preview.linked_principals.count} linked principal(s)")
    if preview.routines.count > 0:
        parts.append(f"{preview.routines.count} routine(s)")
    if preview.user_configs.count > 0:
        parts.append(f"{preview.user_configs.count} user config row(s)")
    if preview.user_skills.count > 0:
        parts.append(f"{preview.user_skills.count} synced skill(s)")
    if preview.github_credentials.count > 0:
        parts.append(f"{preview.github_credentials.count} GitHub credential(s)")
    if preview.github_oauth_states.count > 0:
        parts.append(f"{preview.github_oauth_states.count} OAuth handshake record(s)")
    if preview.mcp_tokens.count > 0:
        parts.append(f"{preview.mcp_tokens.count} MCP token(s)")
    if preview.agent_github_binding.count > 0:
        parts.append(f"{preview.agent_github_binding.count} per-agent GitHub link(s)")
    return ", ".join(parts) if parts else "nothing visible to you yet"


def build_privacy_main_container(
    preview: PurgePreview, *, user_name: str
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Main panel V2 container — no accent, three trust-model groups.

    Pure: no I/O, no ActionRows. The view shell appends hairline + ActionRow.
    """
    body_rows: list[str] = [
        "🪪 **What we hold (our DB)**",
        "-# Identity links (Discord/CLI principals under your account)",
        "-# Routines you scheduled",
        "-# User config rows",
        "-# Synced skill ledger rows",
        "-# Encrypted GitHub credentials (token stored encrypted-at-rest in our DB)",
        "-# GitHub OAuth handshake records",
        "-# The account row itself",
        "",
        "🔐 **What lives in Managed Agents**",
        "-# Agent definitions, system prompts, MCP credentials",
        "-# Session transcripts, turn message content",
        "-# Skill repo references (the repos themselves stay on GitHub)",
        "-# Retention is governed by Anthropic's Managed Agents policy.",
        "",
        "🚫 **What we don't hold**",
        "-# Plaintext credentials (GitHub tokens are encrypted-at-rest in our DB)",
        "-# Message content (we only log structural events)",
    ]
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header(
            "🔒 Privacy",
            subtext=f"for **{user_name}** — daimon holds: {_summary_line(preview)}",
        ),
        layout.hairline(),
        discord.ui.TextDisplay("\n".join(body_rows)),
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
        preview: PurgePreview,
    ) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.account_id = account_id
        self.allowed_user_id = allowed_user_id
        self.user_name = user_name
        self._preview = preview

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

        delete_btn: discord.ui.Button[PrivacyPanelView] = discord.ui.Button(
            label="🗑 Delete…",
            style=discord.ButtonStyle.danger,
        )
        delete_btn.callback = self._on_delete  # type: ignore[method-assign]

        done_btn: discord.ui.Button[PrivacyPanelView] = discord.ui.Button(
            label="✓ Done",
            style=discord.ButtonStyle.secondary,
        )
        done_btn.callback = self._on_done  # type: ignore[method-assign]

        action_row: discord.ui.ActionRow[PrivacyPanelView] = discord.ui.ActionRow(
            policy_btn, export_btn, delete_btn, done_btn
        )
        container = build_privacy_main_container(preview, user_name=user_name)
        container.add_item(layout.hairline())
        container.add_item(action_row)
        self.add_item(container)

    async def _on_export(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "📤 **Export** is not yet implemented.\n\n"
            "When ready, this will produce a JSON dump of every daimon-side row "
            "tied to your identity and either attach it here or DM you a 7-day "
            "signed URL.",
            ephemeral=True,
        )

    async def _on_delete(self, interaction: discord.Interaction) -> None:
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
                build_privacy_main_container(self._preview, user_name=self.user_name)
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
