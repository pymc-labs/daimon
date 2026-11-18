"""`/model` — show or switch the model this conversation runs on.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.

Run inside a conversation thread. With no choice it names the thread's model;
with one it keeps that choice for every later reply (`daimon.core.thread_models`).
Anyone who may start a turn in the thread may switch it, decided by the same
access policy check admission makes. Where provider choice is on, the choices are
the catalog with prices, and a pick applies at once; the reply names anything off on the
new provider (`daimon.core.thread_model_switch.pick_thread`). Asking in words reaches the
same setting through the agent's `set_thread_model` tool.
"""

import uuid
from typing import cast

from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core import backend_capture, channel_backend
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import build_subject
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.thread_model_flow import NO_AGENT, thread_pick_result
from daimon.core.thread_model_switch import (
    ThreadPick,
    pick_thread,
    pick_value,
    thread_model_picker,
    thread_pick_for,
)
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
_LEGACY = (
    app_commands.Choice(name="Haiku", value="haiku"),
    app_commands.Choice(name="Sonnet", value="sonnet"),
    app_commands.Choice(name="Opus", value="opus"),
    app_commands.Choice(name="Agent default", value="default"),
)
_AUTOCOMPLETE_LIMIT = 25


def _runtime_of(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]


async def thread_pick_of(
    interaction: BotInteraction, thread: discord.Thread, *, admin: bool
) -> ThreadPick | None:
    """The pick flow for this person in `thread`: their account, rights and agent."""
    runtime = _runtime_of(interaction)
    tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
    if tenant_id is None:
        return None
    async with runtime.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform="discord",
            external_id=str(interaction.user.id),
        )
        await session.commit()
    subject = build_subject(is_admin=admin, platform_user_id=str(interaction.user.id))
    actor = channel_backend.BackendActor(
        tenant_id=tenant_id, account_id=principal.account_id, subject=subject
    )
    service = channel_backend.make_backend_service(
        runtime.sessionmaker,
        runtime.settings.turn,
        capture_source=backend_capture.for_agent(
            runtime.turn_deps, account_id=principal.account_id, subject=subject
        ),
    )
    return await thread_pick_for(
        service=service,
        anthropic=runtime.anthropic,
        sessionmaker=runtime.sessionmaker,
        settings=runtime.settings,
        default=runtime.deployment_default,
        actor=actor,
        platform="discord",
        channel_id=str(thread.parent_id),
        thread_id=str(thread.id),
        platform_user_id=str(interaction.user.id),
    )


async def may_switch_thread(
    interaction: BotInteraction, tenant_id: uuid.UUID, thread: discord.Thread
) -> bool:
    """Whether this person may start a turn in `thread`, so may switch its model."""
    runtime = _runtime_of(interaction)
    parent = thread.parent
    category_id = getattr(parent, "category_id", None)
    return await may_choose_thread_model(
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
    )


@app_commands.guild_only()
class ModelCog(commands.Cog):
    """Per-conversation model choice."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="model", description=DESCRIPTIONS["model"])
    @app_commands.describe(choice="Leave empty to see the current model")
    @require_registered_guild
    async def model(self, interaction: BotInteraction, choice: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            runtime = _runtime_of(interaction)
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
                if not await may_switch_thread(interaction, tenant_id, thread):
                    raise UserFacingError(_NOT_ALLOWED)
                text = await self._pick(interaction, tenant_id, thread, choice)
            await interaction.edit_original_response(
                content=text, allowed_mentions=discord.AllowedMentions.none()
            )
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            await interaction.edit_original_response(
                content=render_error(exc, request_id=generate_request_id()),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _pick(
        self, interaction: BotInteraction, tenant_id: uuid.UUID, thread: discord.Thread, choice: str
    ) -> str:
        """Keep the pick at once; the note names anything off on the new provider."""
        runtime = _runtime_of(interaction)
        word = choice.strip().lower()
        legacy = word == "default" or not channel_backend.backend_selection_enabled(
            runtime.settings.turn
        )
        if legacy:
            change = await choose_thread_model(
                runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=str(thread.id),
                choice=choice,
            )
            return change.note
        admin = await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS)
        pick = await thread_pick_of(interaction, thread, admin=admin)
        if pick is None:
            raise UserFacingError(NO_AGENT)
        done = await pick_thread(pick, pick_value(choice), thread_id=str(thread.id), admin=admin)
        return thread_pick_result(done)

    @model.autocomplete("choice")
    async def _choices(
        self, interaction: BotInteraction, current: str
    ) -> list[app_commands.Choice[str]]:
        """The agent's catalog with prices where provider choice is on; the words otherwise."""
        runtime = _runtime_of(interaction)
        typed = current.strip().lower()
        legacy = [c for c in _LEGACY if typed in c.name.lower() or typed in c.value]
        if not channel_backend.backend_selection_enabled(runtime.settings.turn):
            return legacy
        try:
            admin = await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS)
            thread = interaction.channel
            if not isinstance(thread, discord.Thread):
                return legacy
            pick = await thread_pick_of(interaction, thread, admin=admin)
            if pick is None:
                return legacy
            picker = await thread_model_picker(pick)
        except (DaimonError, SQLAlchemyError, discord.HTTPException):
            return legacy
        offered = [
            app_commands.Choice(name=f"{c.label} ({c.price})"[:100], value=c.value)
            for c in picker.choices
            if typed in c.label.lower() or typed in c.value.lower()
        ]
        return [*offered, *legacy[-1:]][:_AUTOCOMPLETE_LIMIT]
