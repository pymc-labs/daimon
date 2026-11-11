"""`/model` — show or switch the model this conversation runs on.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.

Run inside a conversation thread. With no choice it names the thread's model;
with one it keeps that choice for every later reply (`daimon.core.thread_models`).
Anyone who may start a turn in the thread may switch it, decided by the same
access policy check admission makes. Where provider choice is on, the choices are
the catalog with prices, and a pick that changes provider shows what changes and
asks once (`daimon.core.thread_model_switch`). Asking in words reaches the same
setting through the agent's `set_thread_model` tool.
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
from daimon.adapters.discord.views import GuardedView
from daimon.core import backend_capture, channel_backend
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import build_subject
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.model_switch import ModelSwitchPreview, SwitchRefusal, switch_fact_lines
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.thread_model_switch import (
    APPLIES_TO,
    ThreadPick,
    confirm_thread_or_refusal,
    pick_value,
    preview_thread_or_refusal,
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
_CONFIRM_TIMEOUT_SECONDS = 300.0


class _ConfirmView(GuardedView):
    """The one Confirm for a pick that changes what the conversation runs on."""

    def __init__(
        self,
        *,
        allowed_user_id: int,
        pick: ThreadPick,
        value: str,
        shown: ModelSwitchPreview,
        thread_id: str,
        admin: bool,
    ) -> None:
        super().__init__(allowed_user_id=allowed_user_id, timeout=_CONFIRM_TIMEOUT_SECONDS)
        self._pick = pick
        self._value = value
        self._shown = shown
        self._thread_id = thread_id
        self._admin = admin
        self.confirm.disabled = not shown.allowed

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button["_ConfirmView"]
    ) -> None:
        self._finalize()
        await interaction.response.defer()
        done = await confirm_thread_or_refusal(
            self._pick,
            self._value,
            thread_id=self._thread_id,
            digest=self._shown.digest,
            revision=self._shown.revision,
            admin=self._admin,
        )
        text = done.line if isinstance(done, SwitchRefusal) else done.note
        if isinstance(done, SwitchRefusal):
            text += " Nothing changed."
        await interaction.edit_original_response(
            content=text, view=None, allowed_mentions=discord.AllowedMentions.none()
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button["_ConfirmView"]
    ) -> None:
        self._finalize()
        await interaction.response.edit_message(
            content="Nothing changed.", view=None, allowed_mentions=discord.AllowedMentions.none()
        )


def _runtime_of(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]


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
        view: discord.ui.View | None = None
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
                text, view = await self._pick(interaction, tenant_id, thread, choice)
            await interaction.edit_original_response(
                content=text, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            await interaction.edit_original_response(
                content=render_error(exc, request_id=generate_request_id()),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _pick(
        self, interaction: BotInteraction, tenant_id: uuid.UUID, thread: discord.Thread, choice: str
    ) -> tuple[str, discord.ui.View | None]:
        """Keep a pick that needs no consent; otherwise show what changes and ask once."""
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
            return change.note, None
        admin = await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS)
        pick = await self._thread_pick(interaction, thread, admin=admin)
        if pick is None:
            raise UserFacingError("No agent answers in this conversation yet.")
        value = pick_value(choice)
        shown = await preview_thread_or_refusal(pick, value, admin=admin)
        if isinstance(shown, SwitchRefusal):
            return f"{shown.line} Nothing changed.", None
        if shown.preview is None:
            done = await confirm_thread_or_refusal(
                pick,
                value,
                thread_id=str(thread.id),
                digest=shown.digest,
                revision=shown.revision,
                admin=admin,
            )
            return (done.note, None) if not isinstance(done, SwitchRefusal) else (done.line, None)
        lines = switch_fact_lines(shown, agent_name=None, applies_to=APPLIES_TO)
        text = "\n".join((*lines, "Confirm to switch this conversation."))
        view = _ConfirmView(
            allowed_user_id=interaction.user.id,
            pick=pick,
            value=value,
            shown=shown,
            thread_id=str(thread.id),
            admin=admin,
        )
        return text, view

    async def _thread_pick(
        self, interaction: BotInteraction, thread: discord.Thread, *, admin: bool
    ) -> ThreadPick | None:
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
            pick = await self._thread_pick(interaction, thread, admin=admin)
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
