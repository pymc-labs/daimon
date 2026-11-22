"""`/model` — show or switch the model and effort this conversation runs on.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection
for some signatures, so this module does NOT enable annotations futures.

Run inside a conversation thread or a direct message. With no choice it names the
conversation's model; with one it keeps that choice for every later reply
(`daimon.core.thread_models`), and `effort` keeps a reasoning effort the same way.
Anyone who may start a turn in the thread may switch it, decided by the same
access policy check admission makes. Where provider choice is on, the choices are
the catalog with prices, and a pick applies at once; the reply names anything off on the
new provider (`daimon.core.thread_model_switch.pick_thread`). Asking in words reaches the
same setting through the agent's `set_thread_model` tool.
"""

import contextlib
import uuid
from dataclasses import dataclass
from typing import Literal, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.checks import (
    is_guild_admin,
    require_registered_guild_or_dm,
    resolve_tenant_for_interaction,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.reactions import redraw_model_marks
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core import backend_capture, channel_backend
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import build_subject
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.model_reactions import model_marks, swapped_marks
from daimon.core.platform_words import DESCRIPTIONS
from daimon.core.stores.direct_messages import get_conversation
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_models import get_thread_choice, get_thread_effort
from daimon.core.stores.thread_sessions import last_model_marks, record_model_marks
from daimon.core.thread_model_flow import (
    NO_AGENT,
    NOTHING_CHANGED,
    ThreadOption,
    thread_options,
    thread_pick_result,
)
from daimon.core.thread_model_switch import (
    ThreadPick,
    pick_thread,
    pick_value,
    thread_model_picker,
    thread_pick_for,
)
from daimon.core.thread_models import (
    EFFORT_NOT_CLAUDE,
    EFFORT_OFF_CLAUDE,
    THREAD_EFFORTS,
    THREAD_MODEL_CHOICES,
    ThreadModelChange,
    ThreadModelRefused,
    choose_thread_model,
    describe_thread_model,
    may_choose_thread_model,
    model_efforts,
    model_label,
    resolve_effort,
)
from mux.contracts.config import DEFAULT_BACKEND
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]
log = structlog.get_logger()

_NOT_IN_THREAD = "Use /model inside a conversation thread or a direct message."
_NOT_HERE = "This bot isn't set up to answer in this server."
_NOT_ALLOWED = "Only people who can start a conversation here can change its model."
_LEGACY = (
    app_commands.Choice(name="Haiku", value="haiku"),
    app_commands.Choice(name="Sonnet", value="sonnet"),
    app_commands.Choice(name="Opus", value="opus"),
    app_commands.Choice(name="Fable", value="fable"),
    app_commands.Choice(name="Agent default", value="default"),
)
_AUTOCOMPLETE_LIMIT = 25


def _runtime_of(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class _Place:
    """Where a pick lands: a thread, or the one private conversation of a DM."""

    tenant_id: uuid.UUID
    channel_id: str
    conversation_id: str
    thread: discord.Thread | None


async def _place_of(interaction: BotInteraction) -> _Place | None:
    """The conversation this command ran in; None outside a thread or a DM with one."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
        if tenant_id is None:
            return None
        return _Place(tenant_id, str(channel.parent_id), str(channel.id), channel)
    if interaction.guild_id is not None or not isinstance(channel, discord.DMChannel):
        return None
    async with _runtime_of(interaction).sessionmaker() as session:
        current = await get_conversation(
            session,
            platform="discord",
            route_key=str(channel.id),
            external_user_id=str(interaction.user.id),
        )
    if current is None:
        return None
    return _Place(current.tenant_id, current.channel_id, current.scope_id, None)


async def thread_pick_of(
    interaction: BotInteraction, thread: discord.Thread, *, admin: bool
) -> ThreadPick | None:
    """The pick flow for this person in `thread`: their account, rights and agent."""
    tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
    if tenant_id is None:
        return None
    return await _pick_of(
        interaction,
        _Place(tenant_id, str(thread.parent_id), str(thread.id), thread),
        admin=admin,
    )


async def _pick_in(interaction: BotInteraction, place: _Place, *, admin: bool) -> ThreadPick | None:
    if place.thread is not None:
        return await thread_pick_of(interaction, place.thread, admin=admin)
    return await _pick_of(interaction, place, admin=admin)


async def _pick_of(interaction: BotInteraction, place: _Place, *, admin: bool) -> ThreadPick | None:
    runtime = _runtime_of(interaction)
    tenant_id = place.tenant_id
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
        channel_id=place.channel_id,
        thread_id=place.conversation_id,
        platform_user_id=str(interaction.user.id),
    )


async def _off_claude(pick: ThreadPick) -> bool:
    """Whether the conversation's agent runs on another provider, so it takes no effort."""
    revision = await pick.service.current(pick.agent)
    return revision is not None and revision.backend not in (None, DEFAULT_BACKEND)


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


_Effort = Literal["low", "medium", "high", "xhigh", "max", "default"]
_EFFORT_LABELS = {"xhigh": "Extra high", "default": "Agent default"}
# Discord accepts a pick for 15 minutes after the last interaction; the menus go a bit before.
_PICKER_TIMEOUT_S = 14 * 60
_SELECT_MAX = 25


async def _redraw_marks(runtime: DiscordRuntime, place: _Place) -> None:
    """Show the conversation's new model and effort emoji where the last turn showed them."""
    tenant_id, thread_id = place.tenant_id, place.conversation_id
    async with runtime.sessionmaker() as session:
        choice = await get_thread_choice(
            session, tenant_id=tenant_id, platform="discord", thread_id=thread_id
        )
        effort = await get_thread_effort(
            session, tenant_id=tenant_id, platform="discord", thread_id=thread_id
        )
        shown = await last_model_marks(
            session, tenant_id=tenant_id, platform="discord", thread_id=thread_id
        )
    # Another provider's model has no emoji, and the agent's own is not known here.
    off_claude = choice is not None and choice.provider != DEFAULT_BACKEND
    if shown is None or off_claude or (choice is None and effort is None):
        return
    marks = swapped_marks(shown, choice.id if choice is not None else None, effort)
    if marks != shown and await redraw_model_marks(thread_id, marks):
        async with runtime.sessionmaker.begin() as session:
            await record_model_marks(
                session, tenant_id=tenant_id, platform="discord", thread_id=thread_id, marks=marks
            )


def _emoji(value: str, *, effort: bool) -> str | None:
    marks = model_marks(None, value) if effort else model_marks(value, None)
    return marks[0] if marks else None


class _Menu(discord.ui.Select["_ModelPicker"]):
    """The model menu or the effort menu; a pick switches the conversation at once."""

    def __init__(self, kind: Literal["choice", "effort"], options: list[discord.SelectOption]):
        super().__init__(placeholder="Model" if kind == "choice" else "Effort", options=options)
        self.kind = kind

    async def callback(self, interaction: BotInteraction) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.view is not None:
            await self.view.chose(interaction, self.kind, self.values[0])


class _ModelPicker(discord.ui.View):
    """`/model` with no choice: the model and effort as two menus, for the asker only."""

    def __init__(
        self,
        cog: "ModelCog",
        place: _Place,
        options: tuple[ThreadOption, ...],
        shown: BotInteraction,
    ) -> None:
        super().__init__(timeout=_PICKER_TIMEOUT_S)
        self._cog, self._place, self._latest = cog, place, shown
        models = [
            discord.SelectOption(
                label=o.label[:100], value=o.value[:100], emoji=_emoji(o.value, effort=False)
            )
            for o in options[:_SELECT_MAX]
        ]
        efforts = [
            discord.SelectOption(
                label=_EFFORT_LABELS.get(e, e.capitalize()), value=e, emoji=_emoji(e, effort=True)
            )
            for e in (*THREAD_EFFORTS, "default")
        ]
        self.add_item(_Menu("choice", models))
        self.add_item(_Menu("effort", efforts))

    async def chose(self, interaction: BotInteraction, kind: str, value: str) -> None:
        self._latest = interaction
        await interaction.response.defer()
        try:
            text = await self._cog.switch(
                interaction,
                self._place,
                choice=value if kind == "choice" else None,
                effort=value if kind == "effort" else None,
            )
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            text = render_error(exc, request_id=generate_request_id())
        await interaction.edit_original_response(
            content=text, view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def on_timeout(self) -> None:
        with contextlib.suppress(discord.HTTPException):
            await self._latest.edit_original_response(view=None)


class ModelCog(commands.Cog):
    """Per-conversation model choice."""

    def __init__(self, bot: DaimonBot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(name="model", description=DESCRIPTIONS["model"])
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.describe(
        choice="Leave empty to see the current model", effort="How hard the model thinks"
    )
    @require_registered_guild_or_dm
    async def model(
        self,
        interaction: BotInteraction,
        choice: str | None = None,
        effort: _Effort | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not self.bot._allows_guild(interaction.guild_id):  # pyright: ignore[reportPrivateUsage]
                raise UserFacingError(_NOT_HERE)
            runtime = _runtime_of(interaction)
            place = await _place_of(interaction)
            if place is None:
                raise UserFacingError(_NOT_IN_THREAD)
            picker: _ModelPicker | None = None
            if choice is None and effort is None:
                text = await describe_thread_model(
                    runtime.sessionmaker,
                    tenant_id=place.tenant_id,
                    platform="discord",
                    thread_id=place.conversation_id,
                )
                picker = await self._picker(interaction, place)
            else:
                text = await self.switch(interaction, place, choice, effort)
            await interaction.edit_original_response(
                content=text,
                allowed_mentions=discord.AllowedMentions.none(),
                view=picker,
            )
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            await interaction.edit_original_response(
                content=render_error(exc, request_id=generate_request_id()),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _picker(self, interaction: BotInteraction, place: _Place) -> "_ModelPicker | None":
        """The two menus, for someone who may switch this conversation; None otherwise."""
        if place.thread is not None and not await may_switch_thread(
            interaction, place.tenant_id, place.thread
        ):
            return None
        settings = _runtime_of(interaction).settings
        pick = None
        if channel_backend.backend_selection_enabled(settings.turn):
            admin = await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS)
            pick = await _pick_in(interaction, place, admin=admin)
        return _ModelPicker(self, place, await thread_options(settings, pick), interaction)

    async def switch(
        self,
        interaction: BotInteraction,
        place: _Place,
        choice: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Keep a pick from the command or a menu, then redraw the emoji the last turn shows."""
        # A direct message is the person's own conversation: no one else may switch it.
        if place.thread is not None and not await may_switch_thread(
            interaction, place.tenant_id, place.thread
        ):
            raise UserFacingError(_NOT_ALLOWED)
        text = await self._pick(interaction, place, choice, effort)
        try:
            await _redraw_marks(_runtime_of(interaction), place)
        except SQLAlchemyError:  # cosmetic: the pick is kept, the emoji catch up next turn
            log.warning("discord.model_marks_redraw_failed", exc_info=True)
        return text

    async def _pick(
        self, interaction: BotInteraction, place: _Place, choice: str | None, effort: str | None
    ) -> str:
        """Keep the pick at once; the note names anything off on the new provider."""
        runtime = _runtime_of(interaction)
        sessionmaker = runtime.sessionmaker
        word = (choice or "").strip().lower()
        legacy = (
            choice is None
            or word == "default"
            or not channel_backend.backend_selection_enabled(runtime.settings.turn)
        )
        if legacy:
            off = False
            if effort is not None and channel_backend.backend_selection_enabled(
                runtime.settings.turn
            ):
                admin = await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS)
                pick = await _pick_in(interaction, place, admin=admin)
                off = pick is not None and await _off_claude(pick)
                if off and choice is None:
                    raise UserFacingError(f"{EFFORT_OFF_CLAUDE} {NOTHING_CHANGED}")
            change = await choose_thread_model(
                sessionmaker,
                tenant_id=place.tenant_id,
                platform="discord",
                thread_id=place.conversation_id,
                choice=choice,
                effort=None if off else effort,
            )
            return f"{change.note} {EFFORT_OFF_CLAUDE}" if off else change.note
        assert choice is not None
        admin = await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS)
        pick = await _pick_in(interaction, place, admin=admin)
        if pick is None:
            raise UserFacingError(NO_AGENT)
        off = False
        if effort is not None:
            # Refuse before the pick is kept: a refusal changes nothing.
            chosen, _reset = resolve_effort(effort)
            model_id = THREAD_MODEL_CHOICES.get(word)
            off = await _off_claude(pick)
            if (
                chosen is not None
                and model_id is not None
                and chosen not in model_efforts(model_id)
                and not off
            ):
                raise ThreadModelRefused(
                    f"{model_label(model_id)} doesn't take an effort. {NOTHING_CHANGED}"
                )
        done = await pick_thread(
            pick, pick_value(choice), thread_id=place.conversation_id, admin=admin
        )
        text = thread_pick_result(done)
        if effort is None or not isinstance(done, ThreadModelChange):
            return text
        picked = done.model_id or "default"
        if ":" in picked and not picked.startswith(f"{DEFAULT_BACKEND}:"):
            return f"{text} {EFFORT_NOT_CLAUDE}"
        if off:
            return f"{text} {EFFORT_OFF_CLAUDE}"
        kept = await choose_thread_model(
            sessionmaker,
            tenant_id=place.tenant_id,
            platform="discord",
            thread_id=place.conversation_id,
            effort=effort,
        )
        return kept.note

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
            place = await _place_of(interaction)
            if place is None:
                return legacy
            pick = await _pick_in(interaction, place, admin=admin)
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
