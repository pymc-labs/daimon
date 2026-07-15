"""Typed-username confirm modal for /privacy delete.

User types `interaction.user.name` verbatim. Mismatch -> ephemeral error, NO
purge, no state change. Match -> defer (ack within Discord's 3s window) ->
purge_account(account_id, anthropic=...) -> post-delete container (green,
controls-less static_view) via edit_original_response.
"""

from __future__ import annotations

import uuid

import anthropic
import structlog
from daimon.adapters.discord import layout
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.privacy_panel.embeds import build_post_delete_container
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.purge import purge_account

import discord

log = structlog.get_logger()


class DeleteConfirmModal(discord.ui.Modal, title="Confirm delete"):
    # No invoker gate needed: Discord modal submissions are inherently bound
    # to the user the modal was sent to.
    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        account_id: uuid.UUID,
        user_name: str,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.account_id = account_id
        self.user_name = user_name
        # Label AND placeholder both show the expected string.
        self.name_in: discord.ui.TextInput[DeleteConfirmModal] = discord.ui.TextInput(
            label=f"Type '{user_name}' to confirm",
            placeholder=user_name,
            required=True,
            max_length=64,
        )
        self.add_item(self.name_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]  # base uses broader Interaction[Client] type
        rid = generate_request_id()
        typed = str(self.name_in.value).strip() if self.name_in.value else ""
        if typed != self.user_name:
            # Mismatch: ephemeral error, NO purge, no state change.
            await interaction.response.send_message(
                f"Username didn't match. Expected `{self.user_name}`, got `{typed}`.\n"
                "Nothing was deleted — re-open the cascade preview to try again.",
                ephemeral=True,
            )
            return
        # Ack within Discord's 3-second window BEFORE the slow purge:
        # purge_account runs a DB transaction plus per-agent session listing
        # and per-session HTTP DELETEs upstream (MA endpoints can hold for
        # tens of seconds). The modal was launched from a component message,
        # so defer() issues a deferred message update; edit_original_response
        # below targets that (ephemeral) panel message via the interaction
        # webhook — same defer-first shape as agent_setup/modals.py.
        await interaction.response.defer()
        try:
            result = await purge_account(
                sm=self.runtime.sessionmaker,
                account_id=self.account_id,
                anthropic=self.runtime.anthropic,
            )
        except (DaimonError, anthropic.APIError) as exc:
            log.warning("privacy.purge.failed", rid=rid, error=str(exc))
            await interaction.followup.send(
                render_error(exc, request_id=rid),
                ephemeral=True,
            )
            return
        # Operator-side log: COUNTS ONLY — no platform_user_id, no user_name, no guild_id.
        log.info(
            "privacy.delete.completed",
            account_id=str(self.account_id),
            principals=result.db.cli_principals + result.db.platform_principals,
            routines=result.db.routines,
            links=result.db.principal_links,
            user_skills=result.db.user_skills,
            github_credentials=result.db.github_credentials,
            oauth_states=result.db.github_oauth_states,
            mcp_tokens=result.db.mcp_tokens,
            agent_github_binding=result.db.agent_github_binding,
            sessions_deleted=result.sessions.deleted,
            sessions_failed=result.sessions.failed,
        )
        await interaction.edit_original_response(
            view=layout.static_view(build_post_delete_container(result)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
