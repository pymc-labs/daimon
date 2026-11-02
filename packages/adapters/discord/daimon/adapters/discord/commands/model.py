"""`/model` — show or switch the model this conversation runs on.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.

Run inside a conversation thread. With no choice it names the thread's model;
with one it keeps that choice for every later reply (`daimon.core.thread_models`).
Anyone who may start a turn in the thread may switch it, decided by the same
access policy check admission makes. Asking in words reaches the same setting
through the agent's `set_thread_model` tool.
"""

from typing import cast

from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.thread_models import (
    choose_thread_model,
    describe_thread_model,
    may_choose_thread_model,
)
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

_NOT_IN_THREAD = "Use /model inside a conversation thread."
_NOT_ALLOWED = "Only people who can start a conversation here can change its model."


@app_commands.guild_only()
class ModelCog(commands.Cog):
    """Per-conversation model choice."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="model", description=DESCRIPTIONS["model"])
    @app_commands.describe(choice="Leave empty to see the current model")
    @app_commands.choices(
        choice=[
            app_commands.Choice(name="Haiku", value="haiku"),
            app_commands.Choice(name="Sonnet", value="sonnet"),
            app_commands.Choice(name="Opus", value="opus"),
            app_commands.Choice(name="Agent default", value="default"),
        ]
    )
    @require_registered_guild
    async def model(
        self, interaction: BotInteraction, choice: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
            tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
            thread = interaction.channel
            if tenant_id is None or not isinstance(thread, discord.Thread):
                raise UserFacingError(_NOT_IN_THREAD)
            if choice is None:
                text = await describe_thread_model(
                    runtime.sessionmaker,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                )
            else:
                parent = thread.parent
                category_id = getattr(parent, "category_id", None)
                if not await may_choose_thread_model(
                    runtime.sessionmaker,
                    runtime.turn_deps.membership_resolver,
                    tenant_id=tenant_id,
                    platform="discord",
                    user_id=str(interaction.user.id),
                    is_admin=await is_guild_admin(interaction, action=DelegatedAction.MODEL),
                    channel_id=str(thread.parent_id),
                    thread_id=str(thread.id),
                    category_id=str(category_id) if category_id is not None else None,
                    category_unresolved=parent is None,
                ):
                    raise UserFacingError(_NOT_ALLOWED)
                change = await choose_thread_model(
                    runtime.sessionmaker,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                    choice=choice.value,
                )
                text = change.note
            await interaction.edit_original_response(
                content=text, allowed_mentions=discord.AllowedMentions.none()
            )
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            await interaction.edit_original_response(
                content=render_error(exc, request_id=generate_request_id()),
                allowed_mentions=discord.AllowedMentions.none(),
            )
